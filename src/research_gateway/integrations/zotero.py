from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from research_gateway.config import ZoteroSettings
from research_gateway.db.database import EvidenceDatabase, normalize_doi, normalize_text
from research_gateway.sources.base import (
    ProviderConfigurationError,
    ProviderError,
    ProviderPayloadError,
    safe_http_error,
)


class ZoteroWriteError(ProviderError):
    """Safe failure from a Zotero collection or bibliographic-item write."""

    error_type = "zotero_write_error"

    def __init__(self, *, status_code: int, operation: str, stage: str) -> None:
        category = _write_error_category(status_code)
        super().__init__(
            "Zotero write failed: "
            f"HTTP {status_code}; category={category}; "
            f"operation={operation}; stage={stage}.",
            status_code=status_code,
        )
        self.category = category
        self.operation = operation
        self.stage = stage


class ZoteroSafetyError(ProviderError):
    """A requested Zotero operation was refused by a local safety rule."""

    error_type = "zotero_safety_error"


class ZoteroAdapter:
    """Manage approved bibliography records without uploading files or implicit deletion."""

    def __init__(
        self,
        settings: ZoteroSettings,
        database: EvidenceDatabase,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=30)

    @property
    def status(self) -> dict[str, Any]:
        return {
            "name": "zotero",
            "enabled": self.settings.enabled,
            "configured": self.settings.configured,
            "available": self.settings.enabled and self.settings.configured,
            "read_capabilities": [
                "library_items",
                "collections",
                "credential_permissions",
                "citation_metadata",
                "formatted_citations",
                "evidence_links",
            ],
            "write_capabilities": [
                "create_collection",
                "delete_collection",
                "create_bibliographic_items",
                "delete_bibliographic_item",
                "collection_membership",
                "tags",
                "citation_provenance",
            ],
            "safety": [
                "destructive_dry_run_default",
                "version_protected_writes",
                "non_empty_collection_refusal",
                "no_attachment_delete",
                "no_pdf_upload",
            ],
        }

    async def search_items(self, query: str, *, limit: int = 25) -> dict[str, Any]:
        payload = await self._request(
            "GET", "/items", params={"q": query, "limit": min(max(limit, 1), 100)}
        )
        if not isinstance(payload, list):
            raise ProviderPayloadError("Zotero returned malformed item search results.")
        return {"items": [_compact_item(item) for item in payload if isinstance(item, dict)]}

    async def get_item(self, item_key: str) -> dict[str, Any]:
        raw = await self._get_raw_item(item_key)
        result = _compact_item(raw)
        link = await self.database.get_zotero_link_by_item_key(
            item_key, self.settings.library_type, self.settings.library_id
        )
        result["evidence_id"] = link.get("evidence_id") if link else None
        return result

    async def list_collections(self, *, limit: int = 100) -> dict[str, Any]:
        payload = await self._request(
            "GET", "/collections", params={"limit": min(max(limit, 1), 100)}
        )
        if not isinstance(payload, list):
            raise ProviderPayloadError("Zotero returned malformed collection results.")
        return {
            "collections": [_compact_collection(item) for item in payload if isinstance(item, dict)]
        }

    async def get_collection(self, collection_key: str) -> dict[str, Any]:
        payload = await self._request("GET", f"/collections/{collection_key}")
        if not isinstance(payload, dict):
            raise ProviderPayloadError("Zotero returned malformed collection metadata.")
        return _compact_collection(payload)

    async def credential_status(self) -> dict[str, Any]:
        """Return effective permissions for the configured library without exposing the key."""
        payload = await self._request("GET", "/keys/current", library_scoped=False)
        if not isinstance(payload, dict):
            raise ProviderPayloadError("Zotero returned malformed credential metadata.")
        access = payload.get("access") or {}
        if not isinstance(access, dict):
            raise ProviderPayloadError("Zotero returned malformed credential metadata.")
        if self.settings.library_type == "user":
            permissions = access.get("user") or {}
        else:
            groups = access.get("groups") or {}
            permissions = (
                groups.get(self.settings.library_id) or groups.get("all") or {}
                if isinstance(groups, dict)
                else {}
            )
        if not isinstance(permissions, dict):
            permissions = {}
        return {
            "library_type": self.settings.library_type,
            "library_id": self.settings.library_id,
            "library_read": bool(permissions.get("library")),
            "library_write": bool(permissions.get("write")),
            "notes": bool(permissions.get("notes")),
        }

    async def create_collection(
        self, name: str, *, parent_collection_key: str | None = None
    ) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Collection name must not be empty.")

        async def action() -> dict[str, Any]:
            if parent_collection_key:
                await self.get_collection(parent_collection_key)
                candidates = await self._all_pages(
                    f"/collections/{parent_collection_key}/collections"
                )
            else:
                candidates = await self._all_pages("/collections/top")
            for candidate in candidates:
                compact = _compact_collection(candidate)
                if normalize_text(compact.get("name")) == normalize_text(clean_name):
                    return {**compact, "created": False}
            payload = await self._request(
                "POST",
                "/collections",
                json=[
                    {
                        "name": clean_name,
                        "parentCollection": parent_collection_key or False,
                    }
                ],
                operation="create_collection",
                stage="collection_creation",
            )
            created = _successful_results(payload).get("0")
            collection_key = _created_key(created)
            if not collection_key:
                raise _bulk_write_error(
                    payload,
                    indexes=["0"],
                    operation="create_collection",
                    stage="collection_creation",
                )
            version = created.get("version") if isinstance(created, dict) else None
            return {
                "collection_key": collection_key,
                "key": collection_key,
                "name": clean_name,
                "parent_collection_key": parent_collection_key,
                "parentCollection": parent_collection_key or False,
                "version": version,
                "created": True,
            }

        return await self._audited(
            "zotero.create_collection",
            action,
            entity_type="zotero_collection",
            summary="Created or reused a Zotero collection.",
        )

    async def delete_collection(
        self,
        collection_key: str,
        *,
        dry_run: bool = True,
        recursive: bool = False,
    ) -> dict[str, Any]:
        async def action() -> dict[str, Any]:
            collection = await self.get_collection(collection_key)
            contents = await self._collection_contents(collection_key)
            result = {
                "collection_key": collection_key,
                "name": collection.get("name"),
                "version": collection.get("version"),
                "direct_item_keys": [item.get("item_key") for item in contents["items"]],
                "child_collection_keys": [
                    child.get("collection_key") for child in contents["collections"]
                ],
                "direct_item_count": len(contents["items"]),
                "child_collection_count": len(contents["collections"]),
                "recursive": recursive,
                "dry_run": dry_run,
                "items_deleted": 0,
                "attachments_deleted": 0,
            }
            if dry_run:
                return {**result, "deleted": False, "would_delete": True}
            if (contents["items"] or contents["collections"]) and not recursive:
                raise ZoteroSafetyError(
                    "Zotero collection deletion refused because the collection is not empty; "
                    "inspect the dry run and explicitly set recursive=true to remove collection "
                    "folders while preserving bibliography items."
                )
            deleted_collections: list[str] = []
            preserved_items: set[str] = set()
            await self._delete_collection_tree(
                collection,
                recursive=recursive,
                deleted_collections=deleted_collections,
                preserved_items=preserved_items,
            )
            return {
                **result,
                "deleted": True,
                "deleted_collection_keys": deleted_collections,
                "preserved_item_keys": sorted(preserved_items),
            }

        return await self._audited(
            "zotero.delete_collection",
            action,
            entity_type="zotero_collection",
            entity_id=collection_key,
            summary=(
                "Planned Zotero collection deletion."
                if dry_run
                else "Deleted a Zotero collection with explicit safety controls."
            ),
        )

    async def create_item(
        self,
        *,
        evidence_id: str | None = None,
        title: str | None = None,
        authors: list[dict[str, Any]] | None = None,
        year: str | int | None = None,
        doi: str | None = None,
        url: str | None = None,
        item_type: str | None = None,
        collection_keys: list[str] | None = None,
        tags: list[str] | None = None,
        arxiv_id: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        requested_collections = _unique_text(collection_keys or [])
        requested_tags = _unique_text(tags or [])

        async def action() -> dict[str, Any]:
            for collection_key in requested_collections:
                await self.get_collection(collection_key)
            if evidence_id:
                evidence = await self.database.get_evidence(evidence_id)
                if not evidence:
                    raise KeyError(f"Unknown evidence record: {evidence_id}")
                if not evidence.get("final_corpus") and evidence.get("screening_status") not in {
                    "included",
                    "final",
                }:
                    raise ZoteroSafetyError(
                        "Zotero item creation refused because the evidence is not included "
                        "or final."
                    )
                record = dict(evidence)
                record["arxiv_id"] = arxiv_id or _evidence_arxiv_id(evidence)
            else:
                if not str(title or "").strip():
                    raise ValueError("title is required when evidence_id is not supplied.")
                record = {
                    "evidence_id": "",
                    "title": str(title).strip(),
                    "authors": authors or [],
                    "year": year,
                    "publication_date": str(year or ""),
                    "doi": doi,
                    "normalized_doi": normalize_doi(doi),
                    "url": url or "",
                    "keywords": [],
                    "publication_type": None,
                    "review_status": "unknown",
                    "arxiv_id": arxiv_id,
                }
            existing_link = (
                await self.database.get_zotero_link(
                    evidence_id, self.settings.library_type, self.settings.library_id
                )
                if evidence_id
                else None
            )
            matched_by = "persisted_item_key" if existing_link else None
            remote = (
                await self._get_raw_item(str(existing_link["item_key"]))
                if existing_link
                else await self._find_existing_item(record)
            )
            if remote and not matched_by:
                matched_by = _match_reason(record, remote)
            if remote:
                item_key = _created_key(remote)
                changes = _merged_item_changes(
                    remote, collection_keys=requested_collections, tags=requested_tags
                )
                if dry_run:
                    return {
                        "dry_run": True,
                        "created": False,
                        "matched_existing": True,
                        "matched_by": matched_by,
                        "item_key": item_key,
                        "would_update": changes,
                        "evidence_id": evidence_id,
                        "files_uploaded": 0,
                    }
                if changes:
                    await self._patch_item(
                        remote,
                        changes,
                        operation="update_bibliographic_item",
                        stage="item_update",
                    )
                if evidence_id:
                    await self.database.save_zotero_link(
                        evidence_id,
                        self.settings.library_type,
                        self.settings.library_id,
                        item_key,
                    )
                remote_data = remote.get("data") or {}
                return {
                    "dry_run": False,
                    "created": False,
                    "matched_existing": True,
                    "matched_by": matched_by,
                    "item_key": item_key,
                    "updated": bool(changes),
                    "evidence_id": evidence_id,
                    "collections": changes.get("collections", remote_data.get("collections") or []),
                    "tags": _tag_names(changes.get("tags", remote_data.get("tags"))),
                    "files_uploaded": 0,
                }
            item = _zotero_item(
                record,
                "",
                collection_keys=requested_collections,
                tags=requested_tags,
                item_type=item_type,
            )
            if dry_run:
                return {
                    "dry_run": True,
                    "created": False,
                    "matched_existing": False,
                    "would_create": True,
                    "evidence_id": evidence_id,
                    "planned_item": item,
                    "files_uploaded": 0,
                }
            payload = await self._request(
                "POST",
                "/items",
                json=[item],
                operation="create_bibliographic_item",
                stage="item_creation",
            )
            created = _successful_results(payload).get("0")
            item_key = _created_key(created)
            if not item_key:
                raise _bulk_write_error(
                    payload,
                    indexes=["0"],
                    operation="create_bibliographic_item",
                    stage="item_creation",
                )
            if evidence_id:
                await self.database.save_zotero_link(
                    evidence_id,
                    self.settings.library_type,
                    self.settings.library_id,
                    item_key,
                )
            return {
                "dry_run": False,
                "created": True,
                "matched_existing": False,
                "item_key": item_key,
                "evidence_id": evidence_id,
                "collections": requested_collections,
                "tags": _tag_names(item.get("tags")),
                "files_uploaded": 0,
            }

        return await self._audited(
            "zotero.create_item",
            action,
            entity_type="evidence" if evidence_id else "zotero_item",
            entity_id=evidence_id,
            summary=(
                "Planned idempotent Zotero item creation."
                if dry_run
                else "Created, reused, or updated one Zotero bibliographic item."
            ),
        )

    async def add_item_to_collection(self, item_key: str, collection_key: str) -> dict[str, Any]:
        return await self._update_item_collections(
            item_key, collection_key, add=True, operation="zotero.add_item_to_collection"
        )

    async def remove_item_from_collection(
        self, item_key: str, collection_key: str
    ) -> dict[str, Any]:
        return await self._update_item_collections(
            item_key, collection_key, add=False, operation="zotero.remove_item_from_collection"
        )

    async def add_tags(self, item_key: str, tags: list[str]) -> dict[str, Any]:
        return await self.set_tags(
            item_key,
            tags,
            preserve_existing=True,
            _audit_operation="zotero.add_tags",
        )

    async def remove_tags(self, item_key: str, tags: list[str]) -> dict[str, Any]:
        remove = {tag.casefold() for tag in _unique_text(tags)}

        async def action() -> dict[str, Any]:
            item = await self._get_raw_item(item_key)
            existing = _tag_objects((item.get("data") or {}).get("tags"))
            resulting = [tag for tag in existing if str(tag["tag"]).casefold() not in remove]
            changed = resulting != existing
            if changed:
                await self._patch_item(
                    item,
                    {"tags": resulting},
                    operation="remove_item_tags",
                    stage="tag_update",
                )
            return {
                "item_key": item_key,
                "tags": _tag_names(resulting),
                "changed": changed,
            }

        return await self._audited(
            "zotero.remove_tags",
            action,
            entity_type="zotero_item",
            entity_id=item_key,
            summary="Removed selected Zotero tags while preserving unrelated tags.",
        )

    async def set_tags(
        self,
        item_key: str,
        tags: list[str],
        *,
        preserve_existing: bool = True,
        _audit_operation: str = "zotero.set_tags",
    ) -> dict[str, Any]:
        requested = _unique_text(tags)

        async def action() -> dict[str, Any]:
            item = await self._get_raw_item(item_key)
            existing = _tag_objects((item.get("data") or {}).get("tags"))
            resulting = (
                _merge_tag_objects(existing, requested)
                if preserve_existing
                else [{"tag": tag} for tag in requested]
            )
            changed = resulting != existing
            if changed:
                await self._patch_item(
                    item,
                    {"tags": resulting},
                    operation="set_item_tags",
                    stage="tag_update",
                )
            return {
                "item_key": item_key,
                "tags": _tag_names(resulting),
                "changed": changed,
                "preserved_existing": preserve_existing,
            }

        return await self._audited(
            _audit_operation,
            action,
            entity_type="zotero_item",
            entity_id=item_key,
            summary="Updated Zotero tags with explicit preservation behavior.",
        )

    async def delete_item(self, item_key: str, *, dry_run: bool = True) -> dict[str, Any]:
        async def action() -> dict[str, Any]:
            item = await self._get_raw_item(item_key)
            compact = _compact_item(item)
            children = await self._all_pages(f"/items/{item_key}/children")
            child_summary = [
                {
                    "item_key": _created_key(child),
                    "item_type": (child.get("data") or {}).get("itemType"),
                    "title": (child.get("data") or {}).get("title"),
                }
                for child in children
            ]
            result = {
                "item_key": item_key,
                "title": compact.get("title"),
                "version": compact.get("version"),
                "child_items": child_summary,
                "child_item_count": len(child_summary),
                "dry_run": dry_run,
                "attachments_deleted": 0,
            }
            if dry_run:
                return {**result, "deleted": False, "would_delete": True}
            if children:
                raise ZoteroSafetyError(
                    "Zotero item deletion refused because child notes or attachments exist; "
                    "Research Gateway never deletes them implicitly."
                )
            await self._request(
                "DELETE",
                f"/items/{item_key}",
                headers={"If-Unmodified-Since-Version": str(item.get("version") or 0)},
                operation="delete_bibliographic_item",
                stage="item_deletion",
            )
            removed_links = await self.database.delete_zotero_links_by_item_key(
                item_key, self.settings.library_type, self.settings.library_id
            )
            return {**result, "deleted": True, "removed_local_links": removed_links}

        return await self._audited(
            "zotero.delete_item",
            action,
            entity_type="zotero_item",
            entity_id=item_key,
            summary=(
                "Planned Zotero item deletion."
                if dry_run
                else "Deleted one attachment-free Zotero bibliographic item."
            ),
        )

    async def get_citation_metadata(
        self,
        item_keys: list[str],
        *,
        style: str = "apa",
        locale: str = "en-US",
    ) -> dict[str, Any]:
        keys = _validated_item_keys(item_keys)
        payload = await self._request(
            "GET",
            "/items",
            params={
                "itemKey": ",".join(keys),
                "include": "data,citation,bib",
                "style": style,
                "locale": locale,
                "limit": len(keys),
            },
        )
        if not isinstance(payload, list):
            raise ProviderPayloadError("Zotero returned malformed citation metadata.")
        records = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            item_key = _created_key(item)
            link = await self.database.get_zotero_link_by_item_key(
                item_key, self.settings.library_type, self.settings.library_id
            )
            records.append(_citation_metadata(item, link))
        return {"style": style, "locale": locale, "items": records}

    async def format_citation(
        self, item_keys: list[str], *, style: str = "apa", locale: str = "en-US"
    ) -> dict[str, Any]:
        metadata = await self.get_citation_metadata(item_keys, style=style, locale=locale)
        citations = [item["formatted_citation"] for item in metadata["items"]]
        return {**metadata, "citations": citations}

    async def format_bibliography(
        self, item_keys: list[str], *, style: str = "apa", locale: str = "en-US"
    ) -> dict[str, Any]:
        metadata = await self.get_citation_metadata(item_keys, style=style, locale=locale)
        entries = [item["formatted_bibliography"] for item in metadata["items"]]
        return {**metadata, "bibliography_entries": entries, "bibliography": "\n".join(entries)}

    async def get_link_for_evidence(self, evidence_id: str) -> dict[str, Any]:
        link = await self.database.get_zotero_link(
            evidence_id, self.settings.library_type, self.settings.library_id
        )
        return {"evidence_id": evidence_id, "link": link}

    async def get_link_for_item(self, item_key: str) -> dict[str, Any]:
        link = await self.database.get_zotero_link_by_item_key(
            item_key, self.settings.library_type, self.settings.library_id
        )
        return {"item_key": item_key, "link": link}

    async def record_citation_reference(
        self,
        *,
        manuscript: str,
        item_key: str,
        citation_location: str | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        clean_manuscript = manuscript.strip()
        if not clean_manuscript:
            raise ValueError("manuscript must not be empty.")

        async def action() -> dict[str, Any]:
            item = await self._get_raw_item(item_key)
            link = await self.database.get_zotero_link_by_item_key(
                item_key, self.settings.library_type, self.settings.library_id
            )
            identifier = _best_identifier(item.get("data") or {})
            return await self.database.save_citation_reference(
                manuscript=clean_manuscript,
                citation_location=citation_location,
                library_type=self.settings.library_type,
                library_id=self.settings.library_id,
                item_key=item_key,
                evidence_id=str(link["evidence_id"]) if link else None,
                identifier=identifier,
                rationale=rationale,
            )

        return await self._audited(
            "zotero.record_citation_reference",
            action,
            entity_type="citation_reference",
            entity_id=item_key,
            summary="Recorded manuscript-to-Zotero citation provenance.",
        )

    async def list_citation_references(
        self, *, manuscript: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        return {
            "references": await self.database.list_citation_references(
                manuscript=manuscript, limit=min(max(limit, 1), 500)
            )
        }

    async def sync_final_corpus(
        self,
        *,
        study_id: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        try:
            result = await self._sync_final_corpus(study_id=study_id, dry_run=dry_run)
        except Exception as error:
            if not dry_run:
                await self.database.audit(
                    "zotero.sync_final_corpus",
                    status="failed",
                    study_id=study_id,
                    source="zotero",
                    safe_summary=_safe_error_summary(error),
                    error_type=_error_type(error),
                )
            raise
        if not dry_run:
            await self.database.audit(
                "zotero.sync_final_corpus",
                status="completed",
                study_id=study_id,
                source="zotero",
                safe_summary=(
                    f"Created {result['created']} bibliographic items; "
                    f"matched {result['matched_existing']}; "
                    f"skipped {result['already_linked']} linked items."
                ),
            )
        return result

    async def _sync_final_corpus(
        self,
        *,
        study_id: str | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        self._require_configured()
        page = await self.database.list_evidence(study_id=study_id, final=True, limit=10_000)
        create_items: list[tuple[dict[str, Any], dict[str, Any]]] = []
        matched_items: list[tuple[dict[str, Any], dict[str, Any]]] = []
        linked = 0
        collection_key = self.settings.collection_key
        collection_missing = False
        if not collection_key and self.settings.collection_name:
            collection_key = await self._find_collection(self.settings.collection_name) or ""
            collection_missing = not collection_key
            if collection_missing and not dry_run:
                collection_key = await self._create_collection(self.settings.collection_name)
        for evidence in page.items:
            full_evidence = await self.database.get_evidence(evidence["evidence_id"])
            if full_evidence:
                evidence = full_evidence
            existing = await self.database.get_zotero_link(
                evidence["evidence_id"], self.settings.library_type, self.settings.library_id
            )
            if existing:
                linked += 1
                continue
            remote = await self._find_existing_item(evidence)
            if remote:
                matched_items.append((evidence, remote))
                continue
            create_items.append((evidence, _zotero_item(evidence, collection_key)))
        if dry_run:
            return {
                "dry_run": True,
                "final_evidence_count": page.total,
                "would_create": len(create_items),
                "would_link_existing": len(matched_items),
                "already_linked": linked,
                "would_ensure_collection": collection_missing,
                "deleted": 0,
                "files_uploaded": 0,
            }
        matched_existing = 0
        updated_existing = 0
        for evidence, item in matched_items:
            item_key = _created_key(item)
            if not item_key:
                continue
            changes = _merged_item_changes(
                item, collection_keys=[collection_key] if collection_key else [], tags=[]
            )
            if changes:
                await self._patch_item(
                    item,
                    changes,
                    operation="update_bibliographic_item",
                    stage="item_update",
                )
                updated_existing += 1
            await self.database.save_zotero_link(
                evidence["evidence_id"],
                self.settings.library_type,
                self.settings.library_id,
                item_key,
            )
            matched_existing += 1
        if not create_items:
            return {
                "dry_run": False,
                "created": 0,
                "matched_existing": matched_existing,
                "updated_existing": updated_existing,
                "already_linked": linked,
                "created_items": [],
                "deleted": 0,
                "files_uploaded": 0,
            }
        payload = await self._request(
            "POST",
            "/items",
            json=[item for _, item in create_items],
            operation="create_bibliographic_items",
            stage="item_creation",
        )
        successful = _successful_results(payload)
        created_items = []
        missing_indexes = []
        for index, (evidence, _) in enumerate(create_items):
            item_key = _created_key(successful.get(str(index)))
            if not item_key:
                missing_indexes.append(str(index))
                continue
            await self.database.save_zotero_link(
                evidence["evidence_id"],
                self.settings.library_type,
                self.settings.library_id,
                item_key,
            )
            created_items.append(
                {
                    "evidence_id": evidence["evidence_id"],
                    "item_key": item_key,
                    "title": evidence.get("title") or "Untitled",
                }
            )
        if missing_indexes:
            raise _bulk_write_error(
                payload,
                indexes=missing_indexes,
                operation="create_bibliographic_items",
                stage="item_creation",
            )
        return {
            "dry_run": False,
            "created": len(created_items),
            "created_items": created_items,
            "matched_existing": matched_existing,
            "updated_existing": updated_existing,
            "already_linked": linked,
            "deleted": 0,
            "files_uploaded": 0,
        }

    async def _find_collection(self, name: str) -> str | None:
        existing = await self.list_collections(limit=100)
        for collection in existing["collections"]:
            if normalize_text(collection.get("name")) == normalize_text(name):
                return str(collection["collection_key"])
        return None

    async def _create_collection(self, name: str) -> str:
        payload = await self._request(
            "POST",
            "/collections",
            json=[{"name": name, "parentCollection": False}],
            operation="create_collection",
            stage="collection_creation",
        )
        item_key = _created_key(_successful_results(payload).get("0"))
        if not item_key:
            raise _bulk_write_error(
                payload,
                indexes=["0"],
                operation="create_collection",
                stage="collection_creation",
            )
        return item_key

    async def _find_existing_item(self, evidence: dict[str, Any]) -> dict[str, Any] | None:
        doi = normalize_doi(evidence.get("normalized_doi") or evidence.get("doi"))
        arxiv_id = str(evidence.get("arxiv_id") or _evidence_arxiv_id(evidence) or "").strip()
        title = str(evidence.get("title") or "").strip()
        queries = _unique_text([value for value in [doi, arxiv_id, title] if value])
        for query in queries:
            payload = await self._request(
                "GET",
                "/items",
                params={
                    "q": query,
                    "qmode": "everything",
                    "limit": 25,
                    "itemType": "-attachment",
                },
            )
            if not isinstance(payload, list):
                continue
            for item in payload:
                if isinstance(item, dict) and _match_reason(evidence, item):
                    return item
        return None

    async def _update_item_collections(
        self, item_key: str, collection_key: str, *, add: bool, operation: str
    ) -> dict[str, Any]:
        async def action() -> dict[str, Any]:
            if add:
                await self.get_collection(collection_key)
            item = await self._get_raw_item(item_key)
            existing = _unique_text((item.get("data") or {}).get("collections") or [])
            if add:
                resulting = _unique_text([*existing, collection_key])
            else:
                resulting = [value for value in existing if value != collection_key]
            changed = resulting != existing
            if changed:
                await self._patch_item(
                    item,
                    {"collections": resulting},
                    operation=("add_item_to_collection" if add else "remove_item_from_collection"),
                    stage="collection_membership_update",
                )
            return {
                "item_key": item_key,
                "collection_key": collection_key,
                "collections": resulting,
                "changed": changed,
                "duplicate_item_created": False,
            }

        return await self._audited(
            operation,
            action,
            entity_type="zotero_item",
            entity_id=item_key,
            summary=(
                "Added an existing Zotero item to a collection without duplication."
                if add
                else "Removed a Zotero item from one collection without deleting the item."
            ),
        )

    async def _collection_contents(self, collection_key: str) -> dict[str, list[dict[str, Any]]]:
        items = await self._all_pages(f"/collections/{collection_key}/items")
        collections = await self._all_pages(f"/collections/{collection_key}/collections")
        return {
            "items": [_compact_item(item) for item in items],
            "collections": [_compact_collection(item) for item in collections],
        }

    async def _delete_collection_tree(
        self,
        collection: dict[str, Any],
        *,
        recursive: bool,
        deleted_collections: list[str],
        preserved_items: set[str],
    ) -> None:
        collection_key = str(collection["collection_key"])
        contents = await self._collection_contents(collection_key)
        preserved_items.update(
            str(item["item_key"]) for item in contents["items"] if item.get("item_key")
        )
        if contents["collections"] and not recursive:
            raise ZoteroSafetyError("Recursive collection deletion was not explicitly enabled.")
        for child in contents["collections"]:
            await self._delete_collection_tree(
                child,
                recursive=True,
                deleted_collections=deleted_collections,
                preserved_items=preserved_items,
            )
        await self._request(
            "DELETE",
            f"/collections/{collection_key}",
            headers={"If-Unmodified-Since-Version": str(collection.get("version") or 0)},
            operation="delete_collection",
            stage="collection_deletion",
        )
        deleted_collections.append(collection_key)

    async def _get_raw_item(self, item_key: str) -> dict[str, Any]:
        payload = await self._request("GET", f"/items/{item_key}")
        if not isinstance(payload, dict):
            raise ProviderPayloadError("Zotero returned malformed item metadata.")
        return payload

    async def _patch_item(
        self,
        item: dict[str, Any],
        changes: dict[str, Any],
        *,
        operation: str,
        stage: str,
    ) -> None:
        item_key = _created_key(item)
        await self._request(
            "PATCH",
            f"/items/{item_key}",
            headers={"If-Unmodified-Since-Version": str(item.get("version") or 0)},
            json=changes,
            operation=operation,
            stage=stage,
        )

    async def _all_pages(self, path: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        start = 0
        while True:
            payload = await self._request("GET", path, params={"limit": 100, "start": start})
            if not isinstance(payload, list):
                raise ProviderPayloadError("Zotero returned malformed paginated results.")
            results.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < 100:
                return results
            start += 100

    async def _audited(
        self,
        operation: str,
        action: Callable[[], Awaitable[dict[str, Any]]],
        *,
        entity_type: str,
        summary: str,
        entity_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            result = await action()
        except Exception as error:
            await self.database.audit(
                operation,
                status="failed",
                source="zotero",
                entity_type=entity_type,
                entity_id=entity_id,
                safe_summary=_safe_error_summary(error),
                error_type=_error_type(error),
            )
            raise
        await self.database.audit(
            operation,
            status="completed",
            source="zotero",
            entity_type=entity_type,
            entity_id=entity_id,
            safe_summary=summary,
        )
        return result

    def _require_configured(self) -> None:
        if not self.settings.configured:
            raise ProviderConfigurationError(
                "Zotero credentials and library ID are not configured."
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        library_scoped: bool = True,
        operation: str | None = None,
        stage: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self._require_configured()
        library = f"{self.settings.library_type}s/{self.settings.library_id}"
        target = f"/{library}{path}" if library_scoped else path
        extra_headers = kwargs.pop("headers", {})
        response = await self.client.request(
            method,
            f"{self.settings.base_url.rstrip('/')}{target}",
            headers={
                "Zotero-API-Key": self.settings.api_key.get_secret_value(),
                "Zotero-API-Version": "3",
                **extra_headers,
            },
            **kwargs,
        )
        if not 200 <= response.status_code < 300:
            if operation and stage:
                raise ZoteroWriteError(
                    status_code=response.status_code,
                    operation=operation,
                    stage=stage,
                )
            raise safe_http_error("zotero", response.status_code)
        return response.json() if response.content else {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()


def _zotero_item(
    evidence: dict[str, Any],
    collection_key: str,
    *,
    collection_keys: list[str] | None = None,
    tags: list[str] | None = None,
    item_type: str | None = None,
) -> dict[str, Any]:
    creators = []
    for author in evidence.get("authors") or []:
        if not isinstance(author, dict):
            continue
        name = str(author.get("name") or "").strip()
        first_name = str(author.get("firstName") or author.get("first_name") or "").strip()
        last_name = str(author.get("lastName") or author.get("last_name") or "").strip()
        if name:
            creators.append({"creatorType": "author", "name": name})
        elif first_name or last_name:
            creators.append(
                {"creatorType": "author", "firstName": first_name, "lastName": last_name}
            )
    resolved_type = _resolve_item_type(item_type, evidence.get("publication_type"))
    tag_names = [str(keyword) for keyword in evidence.get("keywords") or []]
    for classification in (evidence.get("publication_type"), evidence.get("review_status")):
        if classification and classification != "unknown":
            tag_names.append(f"research-gateway:{classification}")
    tag_names.extend(tags or [])
    evidence_id = str(evidence.get("evidence_id") or "").strip()
    extra_lines = []
    if evidence_id:
        extra_lines.append(f"Research Gateway evidence ID: {evidence_id}")
    if evidence.get("publication_type"):
        extra_lines.append(f"Publication type: {evidence['publication_type']}")
    if evidence.get("review_status"):
        extra_lines.append(f"Review status: {evidence['review_status']}")
    arxiv_id = str(evidence.get("arxiv_id") or _evidence_arxiv_id(evidence) or "").strip()
    if arxiv_id:
        extra_lines.append(f"arXiv: {arxiv_id}")
    item = {
        "itemType": resolved_type,
        "title": evidence.get("title") or "Untitled",
        "creators": creators,
        "abstractNote": evidence.get("abstract") or "",
        "date": evidence.get("publication_date") or str(evidence.get("year") or ""),
        "url": evidence.get("url") or "",
        "tags": [{"tag": tag} for tag in _unique_text(tag_names)],
        "extra": "\n".join(extra_lines),
    }
    publication = evidence.get("publication") or ""
    source_field = {
        "journalArticle": "publicationTitle",
        "conferencePaper": "proceedingsTitle",
        "bookSection": "bookTitle",
        "book": "publisher",
        "preprint": "repository",
        "report": "institution",
        "thesis": "university",
        "webpage": "websiteTitle",
    }[resolved_type]
    if publication:
        item[source_field] = publication
    doi = normalize_doi(evidence.get("normalized_doi") or evidence.get("doi"))
    if doi and resolved_type != "book":
        item["DOI"] = doi
    memberships = _unique_text(
        [*(collection_keys or []), *([collection_key] if collection_key else [])]
    )
    if memberships:
        item["collections"] = memberships
    return item


def _resolve_item_type(requested: str | None, publication_type: Any) -> str:
    aliases = {
        "journal_article": "journalArticle",
        "journalarticle": "journalArticle",
        "conference_paper": "conferencePaper",
        "conferencepaper": "conferencePaper",
        "book_chapter": "bookSection",
        "booksection": "bookSection",
        "book": "book",
        "preprint": "preprint",
        "report": "report",
        "thesis": "thesis",
        "webpage": "webpage",
    }
    value = str(requested or publication_type or "journal_article").strip()
    resolved = aliases.get(value.casefold().replace(" ", "_"), value)
    allowed = set(aliases.values())
    if resolved not in allowed:
        raise ValueError(f"Unsupported Zotero item_type: {value}")
    return resolved


def _compact_item(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") or {}
    item_key = item.get("key") or data.get("key")
    return {
        "item_key": item_key,
        "key": item_key,
        "version": item.get("version") or data.get("version"),
        "itemType": data.get("itemType"),
        "title": data.get("title"),
        "DOI": data.get("DOI"),
        "date": data.get("date"),
        "url": data.get("url"),
        "creators": data.get("creators") or [],
        "collections": data.get("collections") or [],
        "tags": _tag_names(data.get("tags")),
    }


def _compact_collection(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") or {}
    collection_key = item.get("key") or data.get("key")
    parent = data.get("parentCollection") or None
    return {
        "collection_key": collection_key,
        "key": collection_key,
        "version": item.get("version") or data.get("version"),
        "name": data.get("name"),
        "parent_collection_key": parent,
        "parentCollection": parent or False,
    }


def _citation_metadata(item: dict[str, Any], link: dict[str, Any] | None) -> dict[str, Any]:
    data = item.get("data") or {}
    date = data.get("date")
    return {
        "item_key": _created_key(item),
        "item_type": data.get("itemType"),
        "title": data.get("title"),
        "authors": data.get("creators") or [],
        "year": str(date)[:4] if date else None,
        "journal_or_conference": (
            data.get("publicationTitle")
            or data.get("proceedingsTitle")
            or data.get("bookTitle")
            or data.get("repository")
        ),
        "volume": data.get("volume"),
        "issue": data.get("issue"),
        "pages": data.get("pages"),
        "publisher": data.get("publisher") or data.get("institution"),
        "DOI": data.get("DOI"),
        "URL": data.get("url"),
        "date": date,
        "formatted_citation": item.get("citation") or "",
        "formatted_bibliography": item.get("bib") or "",
        "evidence_id": link.get("evidence_id") if link else None,
        "identifier": _best_identifier(data),
    }


def _merged_item_changes(
    item: dict[str, Any], *, collection_keys: list[str], tags: list[str]
) -> dict[str, Any]:
    data = item.get("data") or {}
    changes: dict[str, Any] = {}
    existing_collections = _unique_text(data.get("collections") or [])
    resulting_collections = _unique_text([*existing_collections, *collection_keys])
    if resulting_collections != existing_collections:
        changes["collections"] = resulting_collections
    existing_tags = _tag_objects(data.get("tags"))
    resulting_tags = _merge_tag_objects(existing_tags, tags)
    if resulting_tags != existing_tags:
        changes["tags"] = resulting_tags
    return changes


def _tag_objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    results = []
    seen: set[str] = set()
    for entry in value:
        if isinstance(entry, dict):
            name = str(entry.get("tag") or "").strip()
            clean = dict(entry)
        else:
            name = str(entry or "").strip()
            clean = {"tag": name}
        if name and name.casefold() not in seen:
            clean["tag"] = name
            results.append(clean)
            seen.add(name.casefold())
    return results


def _tag_names(value: Any) -> list[str]:
    return [str(entry["tag"]) for entry in _tag_objects(value)]


def _merge_tag_objects(
    existing: list[dict[str, Any]], requested: list[str]
) -> list[dict[str, Any]]:
    results = [dict(item) for item in existing]
    seen = {str(item["tag"]).casefold() for item in results}
    for tag in _unique_text(requested):
        if tag.casefold() not in seen:
            results.append({"tag": tag})
            seen.add(tag.casefold())
    return results


def _unique_text(values: list[Any]) -> list[str]:
    results = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text.casefold() not in seen:
            results.append(text)
            seen.add(text.casefold())
    return results


def _evidence_arxiv_id(evidence: dict[str, Any]) -> str | None:
    for identifier in evidence.get("identifiers") or []:
        if str(identifier.get("identifier_type") or "").casefold() in {"arxiv", "arxiv_id"}:
            return str(identifier.get("identifier_value") or "").strip() or None
    for discovery in evidence.get("discoveries") or []:
        if str(discovery.get("provider") or "").casefold() == "arxiv":
            return str(discovery.get("provider_record_id") or "").strip() or None
    return None


def _item_arxiv_id(data: dict[str, Any]) -> str | None:
    for value in (data.get("archiveID"), data.get("extra"), data.get("url")):
        text = str(value or "")
        marker = text.casefold().find("arxiv:")
        if marker >= 0:
            return text[marker + 6 :].strip().split()[0].rstrip("/.,") or None
        if "arxiv.org/abs/" in text.casefold():
            return text.rstrip("/").rsplit("/", 1)[-1]
    return None


def _first_creator(item: dict[str, Any]) -> str:
    creators = (item.get("data") or {}).get("creators") or []
    if not creators:
        return ""
    creator = creators[0]
    if not isinstance(creator, dict):
        return ""
    return normalize_text(
        creator.get("name")
        or " ".join(
            filter(None, [str(creator.get("firstName") or ""), str(creator.get("lastName") or "")])
        )
    )


def _match_reason(evidence: dict[str, Any], item: dict[str, Any]) -> str | None:
    data = item.get("data") or {}
    doi = normalize_doi(evidence.get("normalized_doi") or evidence.get("doi"))
    if doi and normalize_doi(data.get("DOI")) == doi:
        return "doi"
    arxiv_id = str(evidence.get("arxiv_id") or _evidence_arxiv_id(evidence) or "").casefold()
    if arxiv_id and str(_item_arxiv_id(data) or "").casefold() == arxiv_id:
        return "arxiv_id"
    title = normalize_text(evidence.get("title"))
    candidate_title = normalize_text(data.get("title"))
    year = str(evidence.get("year") or evidence.get("publication_date") or "")[:4]
    candidate_year = str(data.get("date") or "")[:4]
    authors = evidence.get("authors") or []
    first_author = ""
    if authors and isinstance(authors[0], dict):
        first_author = normalize_text(
            authors[0].get("name")
            or " ".join(
                filter(
                    None,
                    [
                        str(authors[0].get("firstName") or ""),
                        str(authors[0].get("lastName") or ""),
                    ],
                )
            )
        )
    same_year = not year or not candidate_year or year == candidate_year
    candidate_author = _first_creator(item)
    same_author = not first_author or not candidate_author or first_author == candidate_author
    if title and candidate_title == title and same_year and same_author:
        return "bibliographic_identity"
    return None


def _best_identifier(data: dict[str, Any]) -> str | None:
    doi = normalize_doi(data.get("DOI"))
    if doi:
        return f"doi:{doi}"
    arxiv_id = _item_arxiv_id(data)
    return f"arxiv:{arxiv_id}" if arxiv_id else None


def _validated_item_keys(item_keys: list[str]) -> list[str]:
    keys = _unique_text(item_keys)
    if not keys:
        raise ValueError("At least one Zotero item key is required.")
    if len(keys) > 50:
        raise ValueError("Zotero accepts at most 50 item keys per citation request.")
    return keys


def _successful_results(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    results = payload.get("successful") or payload.get("success") or {}
    return results if isinstance(results, dict) else {}


def _created_key(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    data = value.get("data") or {}
    key = value.get("key") or (data.get("key") if isinstance(data, dict) else None)
    return str(key or "")


def _bulk_write_error(
    payload: Any,
    *,
    indexes: list[str],
    operation: str,
    stage: str,
) -> ZoteroWriteError:
    failed = payload.get("failed") if isinstance(payload, dict) else None
    status_code = 502
    if isinstance(failed, dict):
        for index in indexes:
            detail = failed.get(index)
            if isinstance(detail, dict):
                try:
                    status_code = int(detail.get("code") or 400)
                except (TypeError, ValueError):
                    status_code = 400
                break
    return ZoteroWriteError(status_code=status_code, operation=operation, stage=stage)


def _safe_error_summary(error: Exception) -> str:
    if isinstance(error, ProviderError):
        return error.safe_message
    return "Zotero operation failed during local processing."


def _error_type(error: Exception) -> str:
    return error.error_type if isinstance(error, ProviderError) else type(error).__name__


def _write_error_category(status_code: int) -> str:
    if status_code in {401, 403}:
        return "permission_error"
    if status_code == 400:
        return "validation_error"
    if status_code == 409:
        return "library_locked"
    if status_code == 412:
        return "version_conflict"
    if status_code == 413:
        return "payload_too_large"
    if status_code == 428:
        return "version_required"
    if status_code == 429:
        return "rate_limit_error"
    if status_code >= 500:
        return "upstream_error"
    return "provider_error"

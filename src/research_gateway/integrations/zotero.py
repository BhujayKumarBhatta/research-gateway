from __future__ import annotations

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


class ZoteroAdapter:
    """Synchronize final evidence as bibliographic items; never delete or upload files."""

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
            "read_capabilities": ["library_items", "collections", "credential_permissions"],
            "write_capabilities": ["create_collection", "create_bibliographic_items"],
            "safety": ["dry_run_default", "no_delete", "no_pdf_upload"],
        }

    async def search_items(self, query: str, *, limit: int = 25) -> dict[str, Any]:
        payload = await self._request(
            "GET", "/items", params={"q": query, "limit": min(max(limit, 1), 100)}
        )
        return {"items": [_compact_item(item) for item in payload]}

    async def get_item(self, item_key: str) -> dict[str, Any]:
        return _compact_item(await self._request("GET", f"/items/{item_key}"))

    async def list_collections(self, *, limit: int = 100) -> dict[str, Any]:
        payload = await self._request(
            "GET", "/collections", params={"limit": min(max(limit, 1), 100)}
        )
        return {
            "collections": [
                {
                    "key": item.get("key"),
                    "version": item.get("version"),
                    "name": (item.get("data") or {}).get("name"),
                    "parentCollection": (item.get("data") or {}).get("parentCollection"),
                }
                for item in payload
            ]
        }

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
                safe_summary = (
                    error.safe_message
                    if isinstance(error, ProviderError)
                    else "Zotero sync failed during local processing."
                )
                await self.database.audit(
                    "zotero.sync_final_corpus",
                    status="failed",
                    study_id=study_id,
                    source="zotero",
                    safe_summary=safe_summary,
                    error_type=(
                        error.error_type
                        if isinstance(error, ProviderError)
                        else type(error).__name__
                    ),
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
        if not self.settings.configured:
            raise ProviderConfigurationError(
                "Zotero credentials and library ID are not configured."
            )
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
            item_key = str(item.get("key") or "")
            if not item_key:
                continue
            data = item.get("data") or {}
            collections = list(data.get("collections") or [])
            if collection_key and collection_key not in collections:
                await self._request(
                    "PATCH",
                    f"/items/{item_key}",
                    headers={"If-Unmodified-Since-Version": str(item.get("version") or 0)},
                    json={"collections": [*collections, collection_key]},
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
            if str(collection.get("name") or "").casefold() == name.casefold():
                return str(collection["key"])
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
        query = doi or str(evidence.get("title") or "").strip()
        if not query:
            return None
        candidates = await self._request(
            "GET", "/items", params={"q": query, "limit": 25, "itemType": "-attachment"}
        )
        if not isinstance(candidates, list):
            return None
        title = normalize_text(evidence.get("title"))
        year = str(evidence.get("year") or "")
        for item in candidates:
            if not isinstance(item, dict):
                continue
            data = item.get("data") or {}
            candidate_doi = normalize_doi(data.get("DOI"))
            if doi and candidate_doi == doi:
                return item
            candidate_title = normalize_text(data.get("title"))
            candidate_year = str(data.get("date") or "")[:4]
            same_year = not year or not candidate_year or year == candidate_year
            if title and candidate_title == title and same_year:
                return item
        return None

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
        if not self.settings.configured:
            raise ProviderConfigurationError(
                "Zotero credentials and library ID are not configured."
            )
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


def _zotero_item(evidence: dict[str, Any], collection_key: str) -> dict[str, Any]:
    creators = []
    for author in evidence.get("authors") or []:
        name = str(author.get("name") or "").strip()
        if name:
            creators.append({"creatorType": "author", "name": name})
    item_type = {
        "conference_paper": "conferencePaper",
        "book_chapter": "bookSection",
        "book": "book",
        "preprint": "preprint",
    }.get(str(evidence.get("publication_type") or ""), "journalArticle")
    tags = [str(keyword) for keyword in evidence.get("keywords") or []]
    for classification in (
        evidence.get("publication_type"),
        evidence.get("review_status"),
    ):
        if classification:
            tags.append(f"research-gateway:{classification}")
    item = {
        "itemType": item_type,
        "title": evidence.get("title") or "Untitled",
        "creators": creators,
        "abstractNote": evidence.get("abstract") or "",
        "date": evidence.get("publication_date") or str(evidence.get("year") or ""),
        "url": evidence.get("url") or "",
        "tags": [{"tag": tag} for tag in dict.fromkeys(tags)],
        "extra": (
            f"Research Gateway evidence ID: {evidence['evidence_id']}\n"
            f"Publication type: {evidence.get('publication_type') or 'unknown'}\n"
            f"Review status: {evidence.get('review_status') or 'unknown'}"
        ),
    }
    publication = evidence.get("publication") or ""
    source_field = {
        "journalArticle": "publicationTitle",
        "conferencePaper": "proceedingsTitle",
        "bookSection": "bookTitle",
        "book": "publisher",
        "preprint": "repository",
    }[item_type]
    if publication:
        item[source_field] = publication
    doi = evidence.get("normalized_doi") or evidence.get("doi") or ""
    if doi and item_type != "book":
        item["DOI"] = doi
    if collection_key:
        item["collections"] = [collection_key]
    return item


def _compact_item(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") or {}
    item_key = item.get("key") or data.get("key")
    return {
        "item_key": item_key,
        "key": item_key,
        "version": item.get("version"),
        "itemType": data.get("itemType"),
        "title": data.get("title"),
        "DOI": data.get("DOI"),
        "date": data.get("date"),
        "url": data.get("url"),
        "creators": data.get("creators") or [],
        "collections": data.get("collections") or [],
    }


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
    if status_code == 429:
        return "rate_limit_error"
    if status_code >= 500:
        return "upstream_error"
    return "provider_error"

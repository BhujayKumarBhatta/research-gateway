from __future__ import annotations

from typing import Any

import httpx

from research_gateway.config import ZoteroSettings
from research_gateway.db.database import EvidenceDatabase, normalize_doi, normalize_text
from research_gateway.sources.base import ProviderConfigurationError, safe_http_error


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
            "read_capabilities": ["library_items", "collections"],
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

    async def sync_final_corpus(
        self,
        *,
        study_id: str | None = None,
        dry_run: bool = True,
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
                "deleted": 0,
                "files_uploaded": 0,
            }
        payload = await self._request("POST", "/items", json=[item for _, item in create_items])
        successful = payload.get("successful") or {}
        created = 0
        for index, (evidence, _) in enumerate(create_items):
            item = successful.get(str(index))
            if not isinstance(item, dict) or not item.get("key"):
                continue
            await self.database.save_zotero_link(
                evidence["evidence_id"],
                self.settings.library_type,
                self.settings.library_id,
                str(item["key"]),
            )
            created += 1
        await self.database.audit(
            "zotero.sync_final_corpus",
            status="completed",
            source="zotero",
            safe_summary=f"Created {created} bibliographic items; skipped {linked} linked items.",
        )
        return {
            "dry_run": False,
            "created": created,
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
            "POST", "/collections", json=[{"name": name, "parentCollection": False}]
        )
        successful = payload.get("successful") or {}
        created = successful.get("0")
        if not isinstance(created, dict) or not created.get("key"):
            raise RuntimeError("Zotero did not return a key for the created collection.")
        return str(created["key"])

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

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.settings.configured:
            raise ProviderConfigurationError(
                "Zotero credentials and library ID are not configured."
            )
        library = f"{self.settings.library_type}s/{self.settings.library_id}"
        extra_headers = kwargs.pop("headers", {})
        response = await self.client.request(
            method,
            f"{self.settings.base_url.rstrip('/')}/{library}{path}",
            headers={
                "Zotero-API-Key": self.settings.api_key.get_secret_value(),
                "Zotero-API-Version": "3",
                **extra_headers,
            },
            **kwargs,
        )
        if not 200 <= response.status_code < 300:
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
        "publicationTitle": evidence.get("publication") or "",
        "date": evidence.get("publication_date") or str(evidence.get("year") or ""),
        "DOI": evidence.get("normalized_doi") or evidence.get("doi") or "",
        "url": evidence.get("url") or "",
        "tags": [{"tag": tag} for tag in dict.fromkeys(tags)],
        "extra": (
            f"Research Gateway evidence ID: {evidence['evidence_id']}\n"
            f"Publication type: {evidence.get('publication_type') or 'unknown'}\n"
            f"Review status: {evidence.get('review_status') or 'unknown'}"
        ),
    }
    if collection_key:
        item["collections"] = [collection_key]
    return item


def _compact_item(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") or {}
    return {
        "key": item.get("key"),
        "version": item.get("version"),
        "itemType": data.get("itemType"),
        "title": data.get("title"),
        "DOI": data.get("DOI"),
        "date": data.get("date"),
        "url": data.get("url"),
        "creators": data.get("creators") or [],
        "collections": data.get("collections") or [],
    }

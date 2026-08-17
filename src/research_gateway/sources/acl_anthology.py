from __future__ import annotations

import asyncio
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from acl_anthology import Anthology

from research_gateway.config import AclSettings
from research_gateway.domain.models import ProviderRetentionPolicy, SourcePage, SourceRecord
from research_gateway.sources.base import (
    ProviderPayloadError,
    ProviderStatus,
    ProviderUnavailableError,
    SourceAdapter,
)


class AclAnthologyAdapter(SourceAdapter):
    name = "acl_anthology"
    retention_policy = ProviderRetentionPolicy(
        raw_metadata="full",
        abstract_storage="allowed",
        max_page_size=100,
        terms_reference="https://aclanthology.org/info/development/",
    )

    def __init__(self, settings: AclSettings) -> None:
        self.settings = settings

    @property
    def status(self) -> ProviderStatus:
        exists = self.settings.index_path.is_file()
        return ProviderStatus(
            name=self.name,
            enabled=self.settings.enabled,
            configured=True,
            available=self.settings.enabled and exists,
            credential_requirement=None,
            read_capabilities=["count", "search", "refresh_index", "explore", "save"],
            retention_policy=self.retention_policy,
            paging_notes="Searches a local index built from the official ACL Anthology data.",
            unavailable_reason=None if exists else "official_metadata_index_not_built",
        )

    def _load(self) -> dict[str, Any]:
        if not self.settings.index_path.is_file():
            raise ProviderUnavailableError(
                "ACL Anthology official metadata index is not available; "
                "run the index refresh command."
            )
        try:
            payload = json.loads(self.settings.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProviderPayloadError("ACL Anthology index is unreadable.") from exc
        if not isinstance(payload.get("records"), list):
            raise ProviderPayloadError("ACL Anthology index has an unexpected format.")
        return payload

    async def count(self, query: str, *, filters: dict[str, Any] | None = None) -> int:
        return len(self._matching(query))

    async def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
        filters: dict[str, Any] | None = None,
        sort: dict[str, Any] | None = None,
    ) -> SourcePage:
        if not 1 <= limit <= self.retention_policy.max_page_size:
            raise ValueError("ACL Anthology limit must be between 1 and 100.")
        payload = self._load()
        matches = self._matching(query, payload=payload)
        selected = matches[offset : offset + limit]
        records = [self._map_record(item, payload) for item in selected]
        total = len(matches)
        next_offset = offset + len(records) if offset + len(records) < total else None
        return SourcePage(
            provider=self.name,
            provider_query=query,
            total_results=total,
            offset=offset,
            returned_count=len(records),
            next_offset=next_offset,
            records=records,
            pagination={"offset": offset, "limit": limit, "next_offset": next_offset},
            provider_metadata={"index_version": payload.get("version")},
        )

    def _matching(
        self, query: str, *, payload: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        data = payload or self._load()
        terms = _parse_query(query)
        return [record for record in data["records"] if _matches(record, terms)]

    def _map_record(self, item: dict[str, Any], payload: dict[str, Any]) -> SourceRecord:
        acl_id = str(item["id"])
        doi = item.get("doi")
        identifiers = {"acl_id": acl_id}
        if doi:
            identifiers["doi"] = str(doi)
        return SourceRecord(
            provider=self.name,
            provider_record_id=acl_id,
            title=item.get("title"),
            authors=item.get("authors") or [],
            abstract=item.get("abstract"),
            year=item.get("year"),
            publication=str(item.get("venue") or "ACL Anthology"),
            doi=doi,
            url=item.get("url") or f"https://aclanthology.org/{acl_id}/",
            document_type="paper",
            keywords=item.get("keywords") or [],
            identifiers=identifiers,
            raw_metadata={**item, "source": payload.get("source")},
        )

    async def refresh_index(self, repository_path: Path | None = None) -> dict[str, Any]:
        """Clone/update official ACL data and atomically rebuild the local search index."""
        return await asyncio.to_thread(
            build_official_index, self.settings.index_path, repository_path
        )


def build_official_index(index_path: Path, repository_path: Path | None = None) -> dict[str, Any]:
    selected_repo = repository_path or index_path.parent / "official-repository"
    anthology = Anthology.from_repo(path=selected_repo, verbose=False)
    records: list[dict[str, Any]] = []
    for collection in anthology.collections.values():
        for paper in collection.papers():
            if paper.is_deleted or paper.is_frontmatter:
                continue
            volume = paper.parent
            records.append(
                {
                    "id": paper.full_id,
                    "title": paper.title.as_text(),
                    "authors": [{"name": author.name.as_full()} for author in paper.authors],
                    "year": int(volume.year) if str(volume.year).isdigit() else None,
                    "venue": volume.title.as_text(),
                    "doi": paper.doi,
                    "url": paper.web_url,
                    "abstract": paper.abstract.as_text() if paper.abstract else None,
                    "keywords": [],
                }
            )
    payload = {
        "source": "official-acl-anthology",
        "version": datetime.now(UTC).isoformat(),
        "records": records,
    }
    index_path = index_path.expanduser().absolute()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = index_path.with_suffix(index_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(index_path)
    return {"index_path": str(index_path), "record_count": len(records)}


def _parse_query(query: str) -> list[tuple[str | None, str]]:
    terms: list[tuple[str | None, str]] = []
    for token in shlex.split(query):
        if ":" in token:
            field, value = token.split(":", 1)
            if field.casefold() in {"title", "author", "year", "venue", "keyword", "id"}:
                terms.append((field.casefold(), value.casefold()))
                continue
        terms.append((None, token.casefold()))
    return terms


def _matches(record: dict[str, Any], terms: list[tuple[str | None, str]]) -> bool:
    fields = {
        "title": str(record.get("title") or "").casefold(),
        "author": " ".join(
            str(author.get("name") or "") for author in record.get("authors") or []
        ).casefold(),
        "year": str(record.get("year") or "").casefold(),
        "venue": str(record.get("venue") or "").casefold(),
        "keyword": " ".join(str(item) for item in record.get("keywords") or []).casefold(),
        "id": str(record.get("id") or "").casefold(),
    }
    all_text = " ".join(fields.values())
    return all((value in fields[field]) if field else (value in all_text) for field, value in terms)

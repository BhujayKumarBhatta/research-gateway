from __future__ import annotations

import asyncio
from typing import Any

import httpx

from research_gateway.config import ScopusSettings
from research_gateway.domain.models import ProviderRetentionPolicy, SourcePage, SourceRecord
from research_gateway.sources.base import (
    ProviderConfigurationError,
    ProviderPayloadError,
    ProviderStatus,
    ProviderTimeoutError,
    SourceAdapter,
    clean_int,
    clean_year,
    safe_http_error,
)


class ScopusAdapter(SourceAdapter):
    name = "scopus"
    retention_policy = ProviderRetentionPolicy(
        raw_metadata="minimal",
        abstract_storage="restricted",
        max_page_size=25,
        terms_reference="https://dev.elsevier.com/policy.html",
    )

    def __init__(
        self,
        settings: ScopusSettings,
        *,
        client: httpx.AsyncClient | None = None,
        retry_attempts: int = 2,
        retry_delay: float = 0.2,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self.retry_attempts = max(1, retry_attempts)
        self.retry_delay = max(0, retry_delay)

    @property
    def status(self) -> ProviderStatus:
        configured = self.settings.configured
        return ProviderStatus(
            name=self.name,
            enabled=self.settings.enabled,
            configured=configured,
            available=self.settings.enabled and configured,
            credential_requirement="Elsevier Scopus API key",
            read_capabilities=["count", "search", "explore", "save"],
            retention_policy=self.retention_policy,
            paging_notes="Offset paging; gateway page size capped at 25.",
            unavailable_reason=None if configured else "api_key_not_configured",
        )

    def _headers(self) -> dict[str, str]:
        if not self.settings.configured:
            raise ProviderConfigurationError("Scopus API key is not configured.")
        headers = {
            "Accept": "application/json",
            "X-ELS-APIKey": self.settings.api_key.get_secret_value(),
        }
        institutional_token = self.settings.institutional_token.get_secret_value()
        if institutional_token:
            headers["X-ELS-Insttoken"] = institutional_token
        return headers

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        url = self.settings.base_url.rstrip("/") + "/content/search/scopus"
        for attempt in range(self.retry_attempts):
            try:
                response = await self.client.get(url, params=params, headers=self._headers())
            except httpx.TimeoutException as exc:
                if attempt + 1 < self.retry_attempts:
                    await asyncio.sleep(self.retry_delay)
                    continue
                raise ProviderTimeoutError("Scopus request timed out.") from exc
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ProviderPayloadError("Scopus returned malformed JSON.") from exc
                if not isinstance(payload, dict):
                    raise ProviderPayloadError("Scopus returned an unexpected payload.")
                return payload
            error = safe_http_error(self.name, response.status_code)
            if error.retryable and attempt + 1 < self.retry_attempts:
                await asyncio.sleep(self.retry_delay)
                continue
            raise error
        raise ProviderTimeoutError("Scopus request did not complete.")

    async def count(self, query: str, *, filters: dict[str, Any] | None = None) -> int:
        params: dict[str, Any] = {"query": query, "count": 1, "start": 0}
        params.update(self._safe_options(filters or {}))
        payload = await self._request(params)
        search_results = payload.get("search-results")
        if not isinstance(search_results, dict):
            raise ProviderPayloadError("Scopus response is missing search results.")
        total = clean_int(search_results.get("opensearch:totalResults"))
        if total is None:
            raise ProviderPayloadError("Scopus response is missing the total result count.")
        return total

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
            raise ValueError("Scopus limit must be between 1 and 25.")
        params: dict[str, Any] = {"query": query, "count": limit, "start": offset}
        params.update(self._safe_options(filters or {}))
        if sort and sort.get("value"):
            params["sort"] = str(sort["value"])
        payload = await self._request(params)
        search_results = payload.get("search-results")
        if not isinstance(search_results, dict):
            raise ProviderPayloadError("Scopus response is missing search results.")
        total = clean_int(search_results.get("opensearch:totalResults"))
        if total is None:
            raise ProviderPayloadError("Scopus response is missing the total result count.")
        entries = search_results.get("entry") or []
        if not isinstance(entries, list):
            raise ProviderPayloadError("Scopus response entries are malformed.")
        records = [self._map_entry(entry) for entry in entries if isinstance(entry, dict)]
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
            provider_metadata={"api": "Scopus Search API", "retention": "minimal"},
        )

    @staticmethod
    def _safe_options(filters: dict[str, Any]) -> dict[str, Any]:
        allowed = {"view", "date", "content", "subj", "suppressNavLinks"}
        return {
            key: value for key, value in filters.items() if key in allowed and value is not None
        }

    def _map_entry(self, entry: dict[str, Any]) -> SourceRecord:
        eid = _text(entry.get("eid"))
        scopus_id = _text(entry.get("dc:identifier"))
        provider_id = eid or scopus_id or "unknown"
        authors = _map_authors(entry)
        cover_date = _text(entry.get("prism:coverDate"))
        links = entry.get("link") if isinstance(entry.get("link"), list) else []
        url = next(
            (
                _text(link.get("@href"))
                for link in links
                if isinstance(link, dict) and link.get("@ref") == "scopus"
            ),
            None,
        )
        if not url:
            url = next((_text(link.get("@href")) for link in links if isinstance(link, dict)), None)
        identifiers = {}
        if eid:
            identifiers["scopus_eid"] = eid
        if scopus_id:
            identifiers["scopus_id"] = scopus_id.removeprefix("SCOPUS_ID:")
        doi = _text(entry.get("prism:doi"))
        if doi:
            identifiers["doi"] = doi
        return SourceRecord(
            provider=self.name,
            provider_record_id=provider_id,
            title=_text(entry.get("dc:title")),
            authors=authors,
            year=clean_year(cover_date),
            publication_date=cover_date,
            publication=_text(entry.get("prism:publicationName")),
            doi=doi,
            url=url,
            citation_count=clean_int(entry.get("citedby-count")),
            document_type=_text(entry.get("subtypeDescription")) or _text(entry.get("subtype")),
            identifiers=identifiers,
            raw_metadata={
                "eid": eid,
                "scopus_id": identifiers.get("scopus_id"),
                "subtype": _text(entry.get("subtype")),
            },
        )

    async def aclose(self) -> None:
        if self._owns_client or not self.client.is_closed:
            await self.client.aclose()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _map_authors(entry: dict[str, Any]) -> list[dict[str, Any]]:
    raw = entry.get("author")
    authors: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, list):
        for author in raw:
            if not isinstance(author, dict):
                continue
            name = _text(author.get("authname"))
            if not name:
                name = (
                    " ".join(
                        part
                        for part in (
                            _text(author.get("given-name")),
                            _text(author.get("surname")),
                        )
                        if part
                    )
                    or None
                )
            if name:
                item: dict[str, Any] = {"name": name}
                provider_id = _text(author.get("authid") or author.get("auid"))
                if provider_id:
                    item["provider_id"] = provider_id
                authors.append(item)
    if not authors and _text(entry.get("dc:creator")):
        authors.append({"name": _text(entry.get("dc:creator"))})
    return authors

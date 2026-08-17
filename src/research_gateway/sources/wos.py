from __future__ import annotations

from typing import Any

import httpx

from research_gateway.config import WosSettings
from research_gateway.domain.models import ProviderRetentionPolicy, SourcePage, SourceRecord
from research_gateway.sources.base import (
    ProviderConfigurationError,
    ProviderPayloadError,
    ProviderStatus,
    ProviderTimeoutError,
    ProviderUnavailableError,
    SourceAdapter,
    clean_int,
    clean_year,
    safe_http_error,
)


class WosAdapter(SourceAdapter):
    name = "wos"
    retention_policy = ProviderRetentionPolicy(
        raw_metadata="minimal",
        abstract_storage="restricted",
        max_page_size=50,
        terms_reference="https://developer.clarivate.com/apis/wos-starter",
    )

    def __init__(self, settings: WosSettings, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    @property
    def status(self) -> ProviderStatus:
        supported = self.settings.mode == "starter"
        return ProviderStatus(
            name=self.name,
            enabled=self.settings.enabled,
            configured=self.settings.configured,
            available=self.settings.enabled and self.settings.configured and supported,
            credential_requirement="Clarivate Web of Science API key",
            read_capabilities=["count", "search", "explore", "save"],
            retention_policy=self.retention_policy,
            paging_notes="Starter API uses page/limit with a maximum page size of 50.",
            unavailable_reason=(
                "expanded_contract_requires_configured_base_url"
                if not supported
                else (None if self.settings.configured else "api_key_not_configured")
            ),
        )

    def _base_url(self) -> str:
        if self.settings.mode != "starter":
            raise ProviderUnavailableError(
                "Web of Science Expanded mode is not enabled in V0.1 without an explicit contract."
            )
        return self.settings.base_url.rstrip("/") or "https://api.clarivate.com/apis/wos-starter/v1"

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.configured:
            raise ProviderConfigurationError("Web of Science API key is not configured.")
        try:
            response = await self.client.get(
                self._base_url() + "/documents",
                params=params,
                headers={"X-ApiKey": self.settings.api_key.get_secret_value()},
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Web of Science request timed out.") from exc
        if response.status_code != 200:
            raise safe_http_error(self.name, response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderPayloadError("Web of Science returned malformed JSON.") from exc
        if not isinstance(payload, dict):
            raise ProviderPayloadError("Web of Science returned an unexpected payload.")
        return payload

    async def count(self, query: str, *, filters: dict[str, Any] | None = None) -> int:
        payload = await self._request({"q": query, "db": "WOS", "limit": 1, "page": 1})
        total = clean_int((payload.get("metadata") or {}).get("total"))
        if total is None:
            raise ProviderPayloadError("Web of Science response is missing the total count.")
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
            raise ValueError("Web of Science limit must be between 1 and 50.")
        if offset % limit:
            raise ValueError("Web of Science offset must align with the requested page size.")
        params: dict[str, Any] = {
            "q": query,
            "db": (filters or {}).get("db", "WOS"),
            "limit": limit,
            "page": offset // limit + 1,
        }
        if sort and sort.get("field"):
            params["sortField"] = sort["field"]
        payload = await self._request(params)
        metadata = payload.get("metadata") or {}
        total = clean_int(metadata.get("total"))
        hits = payload.get("hits") or []
        if total is None or not isinstance(hits, list):
            raise ProviderPayloadError("Web of Science response has an unexpected format.")
        records = [self._map_hit(item) for item in hits if isinstance(item, dict)]
        next_offset = offset + len(records) if offset + len(records) < total else None
        return SourcePage(
            provider=self.name,
            provider_query=query,
            total_results=total,
            offset=offset,
            returned_count=len(records),
            next_offset=next_offset,
            records=records,
            pagination={"page": params["page"], "limit": limit},
            provider_metadata={"mode": "starter", "retention": "minimal_on_save"},
        )

    def _map_hit(self, item: dict[str, Any]) -> SourceRecord:
        uid = str(item.get("uid") or "unknown")
        names = item.get("names") or {}
        raw_authors = names.get("authors") if isinstance(names, dict) else []
        authors = [
            {"name": str(author.get("displayName") or author.get("wosStandard"))}
            for author in (raw_authors or [])
            if isinstance(author, dict) and (author.get("displayName") or author.get("wosStandard"))
        ]
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        identifiers_raw = (
            item.get("identifiers") if isinstance(item.get("identifiers"), dict) else {}
        )
        doi = identifiers_raw.get("doi")
        identifiers = {"wos_uid": uid}
        if doi:
            identifiers["doi"] = str(doi)
        citations = item.get("citations") if isinstance(item.get("citations"), list) else []
        cited = next(
            (
                clean_int(citation.get("count"))
                for citation in citations
                if isinstance(citation, dict) and citation.get("db") == "WOS"
            ),
            None,
        )
        links = item.get("links") if isinstance(item.get("links"), dict) else {}
        types = item.get("types") if isinstance(item.get("types"), list) else []
        return SourceRecord(
            provider=self.name,
            provider_record_id=uid,
            title=item.get("title"),
            authors=authors,
            year=clean_year(source.get("publishYear") or source.get("publishDate")),
            publication_date=source.get("publishDate"),
            publication=source.get("sourceTitle"),
            doi=doi,
            url=links.get("record"),
            document_type=str(types[0]) if types else None,
            citation_count=cited,
            identifiers=identifiers,
            raw_metadata={"uid": uid, "issn": identifiers_raw.get("issn")},
        )

    async def aclose(self) -> None:
        if not self.client.is_closed:
            await self.client.aclose()

from __future__ import annotations

from typing import Any

import httpx

from research_gateway.config import IeeeSettings
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


class IeeeXploreAdapter(SourceAdapter):
    name = "ieee_xplore"
    retention_policy = ProviderRetentionPolicy(
        raw_metadata="minimal",
        abstract_storage="restricted",
        max_page_size=200,
        terms_reference="https://developer.ieee.org/docs",
    )

    def __init__(self, settings: IeeeSettings, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    @property
    def status(self) -> ProviderStatus:
        reason = None
        if not self.settings.configured:
            reason = "api_key_not_configured"
        elif self.settings.approval_status == "pending":
            reason = "credential_approval_pending"
        elif self.settings.approval_status == "denied":
            reason = "credential_approval_denied"
        return ProviderStatus(
            name=self.name,
            enabled=self.settings.enabled,
            configured=self.settings.configured,
            available=self.settings.enabled and self.settings.configured and self.settings.approved,
            credential_requirement="IEEE Xplore Metadata API key",
            read_capabilities=["count", "search", "explore", "save"],
            retention_policy=self.retention_policy,
            paging_notes="Official Metadata API; start_record is one-based; max page size 200.",
            unavailable_reason=reason,
        )

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.configured:
            raise ProviderConfigurationError("IEEE Xplore API key is not configured.")
        params = {**params, "apikey": self.settings.api_key.get_secret_value(), "format": "json"}
        url = self.settings.base_url.rstrip("/") + "/api/v1/search/articles"
        try:
            response = await self.client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("IEEE Xplore request timed out.") from exc
        if response.status_code != 200:
            raise safe_http_error(self.name, response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderPayloadError("IEEE Xplore returned malformed JSON.") from exc
        if not isinstance(payload, dict):
            raise ProviderPayloadError("IEEE Xplore returned an unexpected payload.")
        return payload

    async def count(self, query: str, *, filters: dict[str, Any] | None = None) -> int:
        params = self._search_params(query, filters=filters)
        params.update({"start_record": 1, "max_records": 1})
        payload = await self._request(params)
        total = clean_int(payload.get("total_records") or payload.get("totalfound"))
        if total is None:
            raise ProviderPayloadError("IEEE Xplore response is missing the total result count.")
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
            raise ValueError("IEEE Xplore limit must be between 1 and 200.")
        params = self._search_params(query, filters=filters)
        params.update({"start_record": offset + 1, "max_records": limit})
        if sort:
            if sort.get("field"):
                params["sort_field"] = sort["field"]
            if sort.get("order"):
                params["sort_order"] = sort["order"]
        payload = await self._request(params)
        total = clean_int(payload.get("total_records") or payload.get("totalfound"))
        articles = payload.get("articles") or []
        if total is None or not isinstance(articles, list):
            raise ProviderPayloadError("IEEE Xplore response has an unexpected format.")
        records = [self._map_article(item) for item in articles if isinstance(item, dict)]
        next_offset = offset + len(records) if offset + len(records) < total else None
        return SourcePage(
            provider=self.name,
            provider_query=query,
            total_results=total,
            offset=offset,
            returned_count=len(records),
            next_offset=next_offset,
            records=records,
            pagination={"start_record": offset + 1, "max_records": limit},
            provider_metadata={
                "retention": "minimal_on_save",
                "query_field": self.settings.query_field,
            },
        )

    def _search_params(
        self, query: str, *, filters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {self.settings.query_field: query}
        allowed = {
            "abstract",
            "affiliation",
            "article_number",
            "article_title",
            "author",
            "doi",
            "end_date",
            "index_terms",
            "isbn",
            "issn",
            "publication_id",
            "publication_title",
            "publication_year",
            "start_date",
            "content_type",
        }
        for key, value in (filters or {}).items():
            if key in allowed and value is not None:
                params[key] = value
        return params

    def _map_article(self, item: dict[str, Any]) -> SourceRecord:
        article_id = str(item.get("article_number") or "unknown")
        authors_container = item.get("authors") or {}
        raw_authors = (
            authors_container.get("authors")
            if isinstance(authors_container, dict)
            else authors_container
        ) or []
        authors = [
            {"name": str(author.get("full_name") or author.get("name"))}
            for author in raw_authors
            if isinstance(author, dict) and (author.get("full_name") or author.get("name"))
        ]
        keywords: list[str] = []
        index_terms = item.get("index_terms")
        if isinstance(index_terms, dict):
            author_terms = index_terms.get("author_terms")
            if isinstance(author_terms, dict) and isinstance(author_terms.get("terms"), list):
                keywords = [str(value) for value in author_terms["terms"]]
        doi = item.get("doi")
        identifiers = {"ieee_article_number": article_id}
        if doi:
            identifiers["doi"] = str(doi)
        return SourceRecord(
            provider=self.name,
            provider_record_id=article_id,
            title=item.get("title") or item.get("article_title"),
            authors=authors,
            abstract=item.get("abstract"),
            year=clean_year(item.get("publication_year")),
            publication_date=item.get("publication_date"),
            publication=item.get("publication_title"),
            doi=doi,
            url=item.get("html_url") or item.get("abstract_url"),
            document_type=item.get("content_type"),
            keywords=keywords,
            citation_count=clean_int(item.get("citing_paper_count")),
            identifiers=identifiers,
            raw_metadata={"article_number": article_id, "access_type": item.get("accessType")},
        )

    async def aclose(self) -> None:
        if not self.client.is_closed:
            await self.client.aclose()

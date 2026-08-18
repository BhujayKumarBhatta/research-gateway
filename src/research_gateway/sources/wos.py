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
    SourceAdapter,
    clean_int,
    clean_year,
    safe_http_error,
)


class WosAdapter(SourceAdapter):
    name = "wos"

    def __init__(self, settings: WosSettings, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self.retention_policy = ProviderRetentionPolicy(
            raw_metadata="minimal",
            abstract_storage="restricted",
            max_page_size=50 if settings.mode == "starter" else 100,
            terms_reference=(
                "https://developer.clarivate.com/apis/wos-starter"
                if settings.mode == "starter"
                else "https://developer.clarivate.com/apis/wos"
            ),
        )

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
            credential_requirement="Clarivate Web of Science API key",
            read_capabilities=["count", "search", "explore", "save"],
            retention_policy=self.retention_policy,
            paging_notes=(
                "Starter API uses page/limit with a maximum page size of 50."
                if self.settings.mode == "starter"
                else (
                    "Expanded API uses one-based firstRecord/count with a maximum page size of 100."
                )
            ),
            unavailable_reason=reason,
        )

    def _base_url(self) -> str:
        if self.settings.base_url:
            return self.settings.base_url.rstrip("/")
        if self.settings.mode == "starter":
            return "https://api.clarivate.com/apis/wos-starter/v2"
        return "https://api.clarivate.com/api/wos"

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.configured:
            raise ProviderConfigurationError("Web of Science API key is not configured.")
        path = "/documents" if self.settings.mode == "starter" else ""
        try:
            response = await self.client.get(
                self._base_url() + path,
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
        filters = filters or {}
        if self.settings.mode == "starter":
            params = {
                "q": query,
                "db": filters.get("db", "WOS"),
                "limit": 1,
                "page": 1,
            }
        else:
            params = {
                "databaseId": filters.get("database_id", "WOS"),
                "usrQuery": query,
                "count": 0,
                "firstRecord": 1,
                "optionView": filters.get("option_view", "SR"),
            }
        payload = await self._request(params)
        total = _total(payload, self.settings.mode)
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
            raise ValueError(
                f"Web of Science limit must be between 1 and {self.retention_policy.max_page_size}."
            )
        if self.settings.mode == "starter" and offset % limit:
            raise ValueError("Web of Science offset must align with the requested page size.")
        filters = filters or {}
        if self.settings.mode == "starter":
            params: dict[str, Any] = {
                "q": query,
                "db": filters.get("db", "WOS"),
                "limit": limit,
                "page": offset // limit + 1,
            }
            allowed_filters = {
                "edition": "edition",
                "publish_time_span": "publishTimeSpan",
                "modified_time_span": "modifiedTimeSpan",
                "detail": "detail",
            }
        else:
            params = {
                "databaseId": filters.get("database_id", "WOS"),
                "usrQuery": query,
                "count": limit,
                "firstRecord": offset + 1,
                "optionView": filters.get("option_view", "SR"),
                "links": "true",
            }
            allowed_filters = {
                "edition": "edition",
                "publish_time_span": "publishTimeSpan",
                "load_time_span": "loadTimeSpan",
                "created_time_span": "createdTimeSpan",
                "modified_time_span": "modifiedTimeSpan",
                "tc_modified_time_span": "tcModifiedTimeSpan",
                "view_field": "viewField",
            }
        for source_name, api_name in allowed_filters.items():
            if filters.get(source_name) is not None:
                params[api_name] = filters[source_name]
        if sort and sort.get("field"):
            params["sortField"] = sort["field"]
        payload = await self._request(params)
        total = _total(payload, self.settings.mode)
        hits = _hits(payload, self.settings.mode)
        if total is None or not isinstance(hits, list):
            raise ProviderPayloadError("Web of Science response has an unexpected format.")
        mapper = (
            self._map_starter_hit if self.settings.mode == "starter" else self._map_expanded_hit
        )
        records = [mapper(item) for item in hits if isinstance(item, dict)]
        next_offset = offset + len(records) if offset + len(records) < total else None
        pagination = (
            {"page": params["page"], "limit": limit}
            if self.settings.mode == "starter"
            else {"first_record": params["firstRecord"], "count": limit}
        )
        return SourcePage(
            provider=self.name,
            provider_query=query,
            total_results=total,
            offset=offset,
            returned_count=len(records),
            next_offset=next_offset,
            records=records,
            pagination=pagination,
            provider_metadata={"mode": self.settings.mode, "retention": "minimal_on_save"},
        )

    def _map_starter_hit(self, item: dict[str, Any]) -> SourceRecord:
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

    def _map_expanded_hit(self, item: dict[str, Any]) -> SourceRecord:
        uid = str(item.get("UID") or item.get("uid") or "unknown")
        static = item.get("static_data") if isinstance(item.get("static_data"), dict) else {}
        summary = static.get("summary") if isinstance(static.get("summary"), dict) else {}
        full = (
            static.get("fullrecord_metadata")
            if isinstance(static.get("fullrecord_metadata"), dict)
            else {}
        )
        titles = (summary.get("titles") or {}).get("title") or []
        titles = _as_list(titles)
        title = _title_value(titles, "item")
        publication = _title_value(titles, "source")
        names = (summary.get("names") or {}).get("name") or []
        authors = [
            {"name": str(name.get("display_name") or name.get("full_name"))}
            for name in _as_list(names)
            if isinstance(name, dict)
            and name.get("role") == "author"
            and (name.get("display_name") or name.get("full_name"))
        ]
        publication_info = summary.get("pub_info") or {}
        doctypes = (summary.get("doctypes") or {}).get("doctype") or []
        normalized_types = (full.get("normalized_doctypes") or {}).get("doctype") or doctypes
        document_types = [str(value) for value in _as_list(normalized_types)]
        keywords = [str(value) for value in _as_list((full.get("keywords") or {}).get("keyword"))]
        dynamic = item.get("dynamic_data") if isinstance(item.get("dynamic_data"), dict) else {}
        cluster = dynamic.get("cluster_related") or {}
        identifier_items = ((cluster.get("identifiers") or {}).get("identifier")) or []
        identifier_map = {
            str(value.get("type") or "").casefold(): str(value.get("value") or "")
            for value in _as_list(identifier_items)
            if isinstance(value, dict) and value.get("type") and value.get("value")
        }
        doi = identifier_map.get("doi")
        citations = ((dynamic.get("citation_related") or {}).get("tc_list") or {}).get(
            "silo_tc"
        ) or []
        cited = next(
            (
                clean_int(value.get("local_count"))
                for value in _as_list(citations)
                if isinstance(value, dict) and value.get("coll_id") == "WOS"
            ),
            None,
        )
        identifiers = {"wos_uid": uid}
        if doi:
            identifiers["doi"] = doi
        return SourceRecord(
            provider=self.name,
            provider_record_id=uid,
            title=title,
            authors=authors,
            year=clean_year(publication_info.get("pubyear")),
            publication_date=publication_info.get("sortdate") or publication_info.get("coverdate"),
            publication=publication,
            doi=doi,
            document_type=document_types[0] if document_types else None,
            keywords=keywords,
            citation_count=cited,
            identifiers=identifiers,
            raw_metadata={"uid": uid, "mode": "expanded"},
        )

    async def aclose(self) -> None:
        if not self.client.is_closed:
            await self.client.aclose()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _title_value(titles: list[Any], kind: str) -> str | None:
    return next(
        (
            str(value.get("content"))
            for value in titles
            if isinstance(value, dict) and value.get("type") == kind and value.get("content")
        ),
        None,
    )


def _total(payload: dict[str, Any], mode: str) -> int | None:
    if mode == "starter":
        return clean_int((payload.get("metadata") or {}).get("total"))
    return clean_int((payload.get("QueryResult") or {}).get("RecordsFound"))


def _hits(payload: dict[str, Any], mode: str) -> list[Any]:
    if mode == "starter":
        hits = payload.get("hits") or []
        return hits if isinstance(hits, list) else []
    records = (((payload.get("Data") or {}).get("Records") or {}).get("records") or {}).get("REC")
    return _as_list(records)

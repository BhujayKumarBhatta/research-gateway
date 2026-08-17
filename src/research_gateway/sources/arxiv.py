from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from research_gateway.config import ArxivSettings
from research_gateway.domain.models import ProviderRetentionPolicy, SourcePage, SourceRecord
from research_gateway.sources.base import (
    ProviderPayloadError,
    ProviderStatus,
    ProviderTimeoutError,
    SourceAdapter,
    clean_int,
    clean_year,
    safe_http_error,
)

ATOM = "{http://www.w3.org/2005/Atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
ARXIV = "{http://arxiv.org/schemas/atom}"


class ArxivAdapter(SourceAdapter):
    name = "arxiv"
    retention_policy = ProviderRetentionPolicy(
        raw_metadata="full",
        abstract_storage="allowed",
        max_page_size=100,
        terms_reference="https://info.arxiv.org/help/api/user-manual.html",
    )

    def __init__(
        self,
        settings: ArxivSettings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        self.sleep = sleep
        self._has_requested = False

    @property
    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            enabled=self.settings.enabled,
            configured=True,
            available=self.settings.enabled,
            credential_requirement=None,
            read_capabilities=["count", "search", "explore", "save"],
            retention_policy=self.retention_policy,
            paging_notes="Official Atom API; consecutive requests are paced by at least 3 seconds.",
        )

    async def _request(self, params: dict[str, Any]) -> bytes:
        if self._has_requested and self.settings.polite_delay_seconds:
            await self.sleep(self.settings.polite_delay_seconds)
        self._has_requested = True
        try:
            response = await self.client.get(self.settings.base_url, params=params)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("arXiv request timed out.") from exc
        if response.status_code != 200:
            raise safe_http_error(self.name, response.status_code)
        return response.content

    async def count(self, query: str, *, filters: dict[str, Any] | None = None) -> int:
        content = await self._request({"search_query": query, "start": 0, "max_results": 1})
        root = self._parse(content)
        total = clean_int(root.findtext(f"{OPENSEARCH}totalResults"))
        if total is None:
            raise ProviderPayloadError("arXiv response is missing the total result count.")
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
            raise ValueError("arXiv limit must be between 1 and 100.")
        params: dict[str, Any] = {
            "search_query": query,
            "start": offset,
            "max_results": limit,
        }
        if sort:
            if sort.get("sort_by"):
                params["sortBy"] = sort["sort_by"]
            if sort.get("sort_order"):
                params["sortOrder"] = sort["sort_order"]
        content = await self._request(params)
        root = self._parse(content)
        total = clean_int(root.findtext(f"{OPENSEARCH}totalResults"))
        if total is None:
            raise ProviderPayloadError("arXiv response is missing the total result count.")
        records = [self._map_entry(entry) for entry in root.findall(f"{ATOM}entry")]
        next_offset = offset + len(records) if offset + len(records) < total else None
        return SourcePage(
            provider=self.name,
            provider_query=query,
            total_results=total,
            offset=offset,
            returned_count=len(records),
            next_offset=next_offset,
            records=records,
            pagination={"start": offset, "max_results": limit, "next_offset": next_offset},
            provider_metadata={"format": "Atom", "retention": "full_metadata"},
        )

    @staticmethod
    def _parse(content: bytes) -> ET.Element:
        try:
            return ET.fromstring(content)
        except ET.ParseError as exc:
            raise ProviderPayloadError("arXiv returned malformed Atom XML.") from exc

    def _map_entry(self, entry: ET.Element) -> SourceRecord:
        raw_id = _xml_text(entry.find(f"{ATOM}id")) or "unknown"
        arxiv_id = raw_id.rstrip("/").rsplit("/", 1)[-1]
        arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
        title = _collapse(_xml_text(entry.find(f"{ATOM}title")))
        abstract = _collapse(_xml_text(entry.find(f"{ATOM}summary")))
        published = _xml_text(entry.find(f"{ATOM}published"))
        doi = _xml_text(entry.find(f"{ARXIV}doi"))
        journal_ref = _xml_text(entry.find(f"{ARXIV}journal_ref"))
        authors = [
            {"name": name}
            for author in entry.findall(f"{ATOM}author")
            if (name := _xml_text(author.find(f"{ATOM}name")))
        ]
        categories = [
            category.attrib["term"]
            for category in entry.findall(f"{ATOM}category")
            if category.attrib.get("term")
        ]
        url = next(
            (
                link.attrib.get("href")
                for link in entry.findall(f"{ATOM}link")
                if link.attrib.get("rel") == "alternate"
            ),
            raw_id,
        )
        identifiers = {"arxiv_id": arxiv_id}
        if doi:
            identifiers["doi"] = doi
        return SourceRecord(
            provider=self.name,
            provider_record_id=arxiv_id,
            title=title,
            authors=authors,
            abstract=abstract,
            year=clean_year(published),
            publication_date=published,
            publication=journal_ref or "arXiv",
            doi=doi,
            url=url,
            document_type="preprint",
            keywords=categories,
            identifiers=identifiers,
            raw_metadata={
                "id": raw_id,
                "updated": _xml_text(entry.find(f"{ATOM}updated")),
                "published": published,
                "journal_ref": journal_ref,
                "categories": categories,
            },
        )

    async def aclose(self) -> None:
        if not self.client.is_closed:
            await self.client.aclose()


def _xml_text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _collapse(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(value.replace("\\n", " ").split())

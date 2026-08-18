from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from research_gateway.config import IeeeSettings, WosSettings
from research_gateway.sources.ieee_xplore import IeeeXploreAdapter
from research_gateway.sources.wos import WosAdapter

FIXTURES = Path(__file__).parents[2] / "fixtures"


@pytest.mark.asyncio
async def test_ieee_uses_official_query_and_one_based_paging() -> None:
    payload = json.loads((FIXTURES / "ieee_search.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/search/articles"
        assert request.url.params["querytext"] == '(fine-tuning AND "failure modes")'
        assert request.url.params["start_record"] == "1"
        assert request.url.params["max_records"] == "1"
        assert request.url.params["apikey"] == "fixture-ieee-key"
        return httpx.Response(200, json=payload)

    adapter = IeeeXploreAdapter(
        IeeeSettings(enabled=True, api_key="fixture-ieee-key"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        page = await adapter.search('(fine-tuning AND "failure modes")', limit=1, offset=0)
    finally:
        await adapter.aclose()

    assert page.total_results == 3
    assert page.records[0].identifiers["ieee_article_number"] == "999"
    assert page.records[0].authors == [{"name": "Ada Lovelace"}]


@pytest.mark.asyncio
async def test_wos_starter_uses_official_header_and_page_contract() -> None:
    payload = json.loads((FIXTURES / "wos_starter_search.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/apis/wos-starter/v1/documents"
        assert request.headers["X-ApiKey"] == "fixture-wos-key"
        assert request.url.params["q"] == 'TS=("fine tuning")'
        assert request.url.params["db"] == "WOS"
        assert request.url.params["limit"] == "1"
        assert request.url.params["page"] == "1"
        return httpx.Response(200, json=payload)

    adapter = WosAdapter(
        WosSettings(
            enabled=True,
            api_key="fixture-wos-key",
            base_url="https://api.clarivate.com/apis/wos-starter/v1",
            approval_status="active",
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        page = await adapter.search('TS=("fine tuning")', limit=1, offset=0)
    finally:
        await adapter.aclose()

    assert page.total_results == 2
    assert page.records[0].provider_record_id == "WOS:000123"
    assert page.records[0].doi == "10.3000/wos"
    assert page.records[0].citation_count == 8


@pytest.mark.asyncio
async def test_wos_expanded_uses_official_query_contract_and_maps_full_record() -> None:
    payload = json.loads((FIXTURES / "wos_expanded_search.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/wos"
        assert request.headers["X-ApiKey"] == "fixture-wos-key"
        assert request.url.params["databaseId"] == "WOS"
        assert request.url.params["usrQuery"] == 'TS=("fine tuning")'
        assert request.url.params["count"] == "2"
        assert request.url.params["firstRecord"] == "3"
        assert request.url.params["optionView"] == "SR"
        assert request.url.params["sortField"] == "PY+D"
        return httpx.Response(200, json=payload)

    adapter = WosAdapter(
        WosSettings(
            enabled=True,
            api_key="fixture-wos-key",
            mode="expanded",
            approval_status="active",
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        page = await adapter.search(
            'TS=("fine tuning")',
            limit=2,
            offset=2,
            filters={"database_id": "WOS", "option_view": "SR"},
            sort={"field": "PY+D"},
        )
    finally:
        await adapter.aclose()

    assert page.total_results == 12
    assert page.pagination == {"first_record": 3, "count": 2}
    assert page.provider_metadata["mode"] == "expanded"
    record = page.records[0]
    assert record.provider_record_id == "WOS:000456"
    assert record.doi == "10.3000/expanded"
    assert record.publication == "Journal of Contract Tests"
    assert record.citation_count == 17


def test_wos_and_ieee_remain_deferred_until_external_approval_is_active() -> None:
    wos = WosAdapter(WosSettings(enabled=True, api_key="configured-but-pending"))
    ieee = IeeeXploreAdapter(IeeeSettings(enabled=True, api_key="configured-but-pending"))
    assert wos.status.configured is True
    assert wos.status.available is False
    assert wos.status.unavailable_reason == "credential_approval_pending"
    assert ieee.status.configured is True
    assert ieee.status.available is False
    assert ieee.status.unavailable_reason == "credential_approval_pending"


@pytest.mark.asyncio
async def test_ieee_supports_official_metadata_query_filters_and_sorting() -> None:
    payload = json.loads((FIXTURES / "ieee_search.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert "querytext" not in params
        assert params["meta_data"] == '(fine-tuning AND "failure modes")'
        assert params["publication_year"] == "2025"
        assert params["content_type"] == "Conferences"
        assert params["sort_field"] == "article_title"
        assert params["sort_order"] == "asc"
        return httpx.Response(200, json=payload)

    adapter = IeeeXploreAdapter(
        IeeeSettings(
            enabled=True,
            api_key="fixture-ieee-key",
            approval_status="active",
            query_field="meta_data",
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        page = await adapter.search(
            '(fine-tuning AND "failure modes")',
            limit=1,
            offset=0,
            filters={"publication_year": 2025, "content_type": "Conferences"},
            sort={"field": "article_title", "order": "asc"},
        )
    finally:
        await adapter.aclose()
    assert page.records[0].abstract is not None
    assert page.provider_metadata["query_field"] == "meta_data"

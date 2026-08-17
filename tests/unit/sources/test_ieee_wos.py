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

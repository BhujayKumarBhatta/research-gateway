from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from research_gateway.config import ArxivSettings
from research_gateway.sources.arxiv import ArxivAdapter

FIXTURES = Path(__file__).parents[2] / "fixtures"


@pytest.mark.asyncio
async def test_arxiv_preserves_query_sort_and_maps_atom() -> None:
    atom = (FIXTURES / "arxiv_feed.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["search_query"] == 'all:"fine tuning"'
        assert request.url.params["start"] == "0"
        assert request.url.params["max_results"] == "1"
        assert request.url.params["sortBy"] == "submittedDate"
        assert request.url.params["sortOrder"] == "descending"
        return httpx.Response(200, content=atom, headers={"Content-Type": "application/atom+xml"})

    adapter = ArxivAdapter(
        ArxivSettings(polite_delay_seconds=0),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        page = await adapter.search(
            'all:"fine tuning"',
            limit=1,
            offset=0,
            sort={"sort_by": "submittedDate", "sort_order": "descending"},
        )
    finally:
        await adapter.aclose()

    assert page.total_results == 12
    assert page.records[0].provider_record_id == "2501.01234"
    assert page.records[0].title == "An arXiv Paper With Spacing"
    assert page.records[0].doi == "10.1000/test"
    assert page.records[0].identifiers["arxiv_id"] == "2501.01234"
    assert page.records[0].authors == [{"name": "Ada Lovelace"}, {"name": "Grace Hopper"}]
    assert page.records[0].keywords == ["cs.CL", "cs.AI"]


@pytest.mark.asyncio
async def test_arxiv_paces_consecutive_calls() -> None:
    atom = (FIXTURES / "arxiv_feed.xml").read_bytes()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    adapter = ArxivAdapter(
        ArxivSettings(polite_delay_seconds=3),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=atom))
        ),
        sleep=sleep,
    )
    try:
        await adapter.count("all:test")
        await adapter.count("all:test")
    finally:
        await adapter.aclose()

    assert sleeps == [3.0]

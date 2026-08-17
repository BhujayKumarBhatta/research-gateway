from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from research_gateway.config import ScopusSettings
from research_gateway.sources.base import (
    ProviderAuthenticationError,
    ProviderPayloadError,
    ProviderRateLimitError,
)
from research_gateway.sources.scopus import ScopusAdapter

FIXTURES = Path(__file__).parents[2] / "fixtures"


def make_adapter(handler: httpx.MockTransport) -> ScopusAdapter:
    settings = ScopusSettings(
        api_key="fixture-scopus-key",
        institutional_token="fixture-inst-token",
    )
    client = httpx.AsyncClient(transport=handler)
    return ScopusAdapter(settings, client=client)


@pytest.mark.asyncio
async def test_count_uses_official_headers_and_small_page() -> None:
    payload = json.loads((FIXTURES / "scopus_search.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/content/search/scopus"
        assert request.url.params["query"] == 'TITLE-ABS-KEY("fine tuning")'
        assert request.url.params["count"] == "1"
        assert request.headers["X-ELS-APIKey"] == "fixture-scopus-key"
        assert request.headers["X-ELS-Insttoken"] == "fixture-inst-token"
        assert request.headers["Accept"] == "application/json"
        return httpx.Response(200, json=payload)

    adapter = make_adapter(httpx.MockTransport(handler))
    try:
        assert await adapter.count('TITLE-ABS-KEY("fine tuning")') == 42
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_search_maps_missing_fields_defensively() -> None:
    payload = json.loads((FIXTURES / "scopus_search.json").read_text(encoding="utf-8"))
    adapter = make_adapter(httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    try:
        page = await adapter.search("TITLE(test)", limit=2, offset=0)
    finally:
        await adapter.aclose()

    assert page.total_results == 42
    assert page.next_offset == 2
    assert page.records[0].provider_record_id == "2-s2.0-123"
    assert page.records[0].doi == "10.1000/TEST"
    assert page.records[0].authors == [
        {"name": "Lovelace A.", "provider_id": "1"},
        {"name": "Grace Hopper", "provider_id": "2"},
    ]
    assert page.records[0].citation_count == 7
    assert page.records[0].url == "https://www.scopus.com/record/123"
    assert page.records[1].year is None
    assert page.records[1].doi is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, ProviderAuthenticationError),
        (403, ProviderAuthenticationError),
        (429, ProviderRateLimitError),
    ],
)
async def test_safe_upstream_errors(status: int, error_type: type[Exception]) -> None:
    adapter = make_adapter(
        httpx.MockTransport(
            lambda request: httpx.Response(
                status, text="fixture-scopus-key fixture-inst-token should never escape"
            )
        )
    )
    try:
        with pytest.raises(error_type) as caught:
            await adapter.count("TITLE(test)")
    finally:
        await adapter.aclose()

    assert "fixture-scopus-key" not in str(caught.value)
    assert "fixture-inst-token" not in str(caught.value)


@pytest.mark.asyncio
async def test_malformed_payload_is_safe_error() -> None:
    adapter = make_adapter(
        httpx.MockTransport(lambda request: httpx.Response(200, json={"unexpected": True}))
    )
    try:
        with pytest.raises(ProviderPayloadError):
            await adapter.count("TITLE(test)")
    finally:
        await adapter.aclose()

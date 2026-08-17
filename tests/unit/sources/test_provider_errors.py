from __future__ import annotations

import httpx
import pytest

from research_gateway.config import AcmSettings, IeeeSettings, WosSettings
from research_gateway.sources.acm_dl import AcmDlAdapter
from research_gateway.sources.base import (
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderPayloadError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUpstreamError,
    clean_int,
    clean_year,
    safe_http_error,
)
from research_gateway.sources.ieee_xplore import IeeeXploreAdapter
from research_gateway.sources.wos import WosAdapter


def test_common_cleaners_and_safe_http_error_taxonomy() -> None:
    assert clean_int("7") == 7
    assert clean_int("bad") is None
    assert clean_year("Published 2025-01") == 2025
    assert clean_year(None) is None
    assert isinstance(safe_http_error("source", 401), ProviderAuthenticationError)
    assert isinstance(safe_http_error("source", 403), ProviderAuthenticationError)
    assert isinstance(safe_http_error("source", 429), ProviderRateLimitError)
    assert isinstance(safe_http_error("source", 503), ProviderUpstreamError)
    assert isinstance(safe_http_error("source", 400), ProviderUpstreamError)


@pytest.mark.asyncio
async def test_unavailable_and_unconfigured_providers_are_explicit() -> None:
    acm = AcmDlAdapter(AcmSettings(enabled=True))
    assert acm.status.unavailable_reason == "official_programmatic_search_not_verified"
    with pytest.raises(ProviderUnavailableError):
        await acm.count("query")
    with pytest.raises(ProviderUnavailableError):
        await acm.search("query", limit=1, offset=0)

    ieee = IeeeXploreAdapter(IeeeSettings(enabled=True))
    assert ieee.status.available is False
    with pytest.raises(ProviderConfigurationError):
        await ieee.count("query")
    await ieee.aclose()

    wos = WosAdapter(WosSettings(enabled=True, mode="expanded", api_key="key"))
    assert wos.status.unavailable_reason == "expanded_contract_requires_configured_base_url"
    with pytest.raises(ProviderUnavailableError):
        await wos.count("query")
    await wos.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_kind", ["ieee", "wos"])
async def test_official_provider_timeout_and_payload_errors(adapter_kind: str) -> None:
    async def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("fixture timeout", request=request)

    timeout_client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    if adapter_kind == "ieee":
        adapter = IeeeXploreAdapter(
            IeeeSettings(enabled=True, api_key="fake"), client=timeout_client
        )
    else:
        adapter = WosAdapter(WosSettings(enabled=True, api_key="fake"), client=timeout_client)
    with pytest.raises(ProviderTimeoutError):
        await adapter.count("query")
    await timeout_client.aclose()

    malformed_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="not-json"))
    )
    if adapter_kind == "ieee":
        malformed = IeeeXploreAdapter(
            IeeeSettings(enabled=True, api_key="fake"), client=malformed_client
        )
    else:
        malformed = WosAdapter(WosSettings(enabled=True, api_key="fake"), client=malformed_client)
    with pytest.raises(ProviderPayloadError):
        await malformed.count("query")
    await malformed_client.aclose()


@pytest.mark.asyncio
async def test_provider_validation_and_missing_totals() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
    )
    ieee = IeeeXploreAdapter(IeeeSettings(enabled=True, api_key="fake"), client=client)
    with pytest.raises(ValueError, match="between 1 and 200"):
        await ieee.search("query", limit=0, offset=0)
    with pytest.raises(ProviderPayloadError, match="total"):
        await ieee.count("query")
    await client.aclose()

    wos_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"metadata": {"total": 1}, "hits": []})
        )
    )
    wos = WosAdapter(WosSettings(enabled=True, api_key="fake"), client=wos_client)
    with pytest.raises(ValueError, match="between 1 and 50"):
        await wos.search("query", limit=51, offset=0)
    with pytest.raises(ValueError, match="align"):
        await wos.search("query", limit=2, offset=1)
    page = await wos.search("query", limit=1, offset=0, sort={"field": "LD+D"})
    assert page.total_results == 1
    await wos_client.aclose()

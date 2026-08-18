from __future__ import annotations

import asyncio
import json
import socket
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import httpx2
import uvicorn
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from research_gateway.api.app import create_app
from research_gateway.config import ConfigError, Settings
from research_gateway.mcp.server import create_mcp_server
from research_gateway.runtime import GatewayRuntime
from research_gateway.tunnel import NgrokTunnel

SCOPUS_ACCEPTANCE_QUERY = 'TITLE-ABS-KEY("instruction fine-tuning")'
WOS_ACCEPTANCE_QUERY = 'TS=("instruction fine-tuning")'
IEEE_ACCEPTANCE_QUERY = '"instruction fine-tuning"'


async def run_live_scopus(settings: Settings) -> None:
    if not settings.scopus.configured:
        raise ConfigError(
            'Scopus is not configured. Add api_key = "..." under [scopus] in the global config.'
        )
    with tempfile.TemporaryDirectory(prefix="research-gateway-scopus-") as directory:
        temporary = settings.model_copy(deep=True)
        temporary.database.path = Path(directory) / "acceptance.db"
        runtime = GatewayRuntime.build(temporary)
        await runtime.start()
        safe_results: list[object] = []
        try:
            async with Client(create_mcp_server(runtime)) as client:
                count = await client.call_tool(
                    "scopus_count", {"provider_query": SCOPUS_ACCEPTANCE_QUERY}
                )
                total = int(count.structured_content["total"])
                if total < 0:
                    raise RuntimeError("Scopus returned an invalid total.")
                sample = await client.call_tool(
                    "scopus_search",
                    {"provider_query": SCOPUS_ACCEPTANCE_QUERY, "limit": 3},
                )
                if sample.is_error or sample.structured_content["returned_count"] < 1:
                    raise RuntimeError(
                        "Scopus returned no sample metadata for the acceptance query."
                    )
                await client.call_tool(
                    "study_create",
                    {
                        "study_id": "live-scopus",
                        "name": "Live Scopus acceptance",
                        "system_test": True,
                    },
                )
                await client.call_tool(
                    "topic_create",
                    {
                        "study_id": "live-scopus",
                        "topic_id": "instruction-fine-tuning",
                        "name": "Instruction fine-tuning",
                    },
                )
                saved = await client.call_tool(
                    "research_save_search",
                    {
                        "study_id": "live-scopus",
                        "topic_id": "instruction-fine-tuning",
                        "provider": "scopus",
                        "search_intent": "Validate live Scopus capture",
                        "provider_query": SCOPUS_ACCEPTANCE_QUERY,
                        "max_records": 3,
                    },
                )
                run = await client.call_tool(
                    "search_run_get",
                    {"search_run_id": saved.structured_content["search_run_id"]},
                )
                evidence = await client.call_tool(
                    "evidence_search", {"study_id": "live-scopus", "limit": 10}
                )
                if not run.structured_content["hits"] or evidence.structured_content["total"] < 1:
                    raise RuntimeError("Live Scopus records were not persisted and retrievable.")
                safe_results.extend(
                    [
                        count.structured_content,
                        sample.structured_content,
                        saved.structured_content,
                        run.structured_content,
                        evidence.structured_content,
                    ]
                )
                _assert_secret_absent(temporary, safe_results, temporary.database.path)
                print(f"Live Scopus total: {total}")
                print(f"Captured records: {saved.structured_content['retrieved_count']}")
                print("LIVE SCOPUS ACCEPTANCE: PASS")
        finally:
            await runtime.aclose()


async def run_live_open(settings: Settings) -> None:
    with tempfile.TemporaryDirectory(prefix="research-gateway-open-") as directory:
        temporary = settings.model_copy(deep=True)
        temporary.database.path = Path(directory) / "acceptance.db"
        runtime = GatewayRuntime.build(temporary)
        await runtime.start()
        try:
            async with Client(create_mcp_server(runtime)) as client:
                arxiv = await client.call_tool(
                    "arxiv_search", {"provider_query": "all:language model", "limit": 1}
                )
                if arxiv.is_error or arxiv.structured_content["returned_count"] < 1:
                    raise RuntimeError("Live arXiv acceptance returned no records.")
                sources = await client.call_tool("source_list")
                acl = next(
                    source
                    for source in sources.structured_content["sources"]
                    if source["name"] == "acl_anthology"
                )
                if not acl["available"]:
                    raise ConfigError(
                        "ACL Anthology index is unavailable. Build the official local "
                        "metadata index first."
                    )
                acl_result = await client.call_tool(
                    "acl_search", {"provider_query": "language", "limit": 1}
                )
                if acl_result.is_error or acl_result.structured_content["returned_count"] < 1:
                    raise RuntimeError("ACL Anthology index returned no records for a broad query.")
                print("LIVE OPEN SOURCES ACCEPTANCE: PASS")
        finally:
            await runtime.aclose()


async def run_live_wos(settings: Settings) -> None:
    for mode in ("starter", "expanded"):
        label = f"WEB OF SCIENCE {mode.upper()}"
        active = (
            settings.wos.enabled
            and settings.wos.configured
            and settings.wos.approved
            and settings.wos.mode == mode
        )
        if not active:
            print(f"{label} LIVE TEST DEFERRED — EXTERNAL APPROVAL PENDING")
            continue
        await _run_live_licensed_provider(
            settings,
            provider="wos",
            tool_name="wos_search",
            query=WOS_ACCEPTANCE_QUERY,
            study_id=f"live-wos-{mode}",
            label=label,
        )


async def run_live_ieee(settings: Settings) -> None:
    label = "IEEE XPLORE"
    if not (
        settings.ieee_xplore.enabled
        and settings.ieee_xplore.configured
        and settings.ieee_xplore.approved
    ):
        print(f"{label} LIVE TEST DEFERRED — EXTERNAL APPROVAL PENDING")
        return
    from research_gateway.operations.logging import configure_logging

    configure_logging(settings)
    await _run_live_licensed_provider(
        settings,
        provider="ieee_xplore",
        tool_name="ieee_search",
        query=IEEE_ACCEPTANCE_QUERY,
        study_id="live-ieee",
        label=label,
    )


async def _run_live_licensed_provider(
    settings: Settings,
    *,
    provider: str,
    tool_name: str,
    query: str,
    study_id: str,
    label: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix=f"research-gateway-{provider}-") as directory:
        temporary = settings.model_copy(deep=True)
        temporary.database.path = Path(directory) / "acceptance.db"
        runtime = GatewayRuntime.build(temporary)
        await runtime.start()
        safe_results: list[object] = []
        try:
            async with Client(create_mcp_server(runtime)) as client:
                sample = await client.call_tool(tool_name, {"provider_query": query, "limit": 1})
                if sample.is_error or sample.structured_content["returned_count"] < 1:
                    raise RuntimeError(f"{label} returned no acceptance metadata.")
                await client.call_tool(
                    "study_create",
                    {"study_id": study_id, "name": f"{label} acceptance", "system_test": True},
                )
                saved = await client.call_tool(
                    "research_save_search",
                    {
                        "study_id": study_id,
                        "provider": provider,
                        "search_intent": f"Validate {label} official API capture",
                        "provider_query": query,
                        "max_records": 1,
                    },
                )
                run = await client.call_tool(
                    "search_run_get",
                    {"search_run_id": saved.structured_content["search_run_id"]},
                )
                if saved.is_error or not run.structured_content["hits"]:
                    raise RuntimeError(f"{label} live result was not saved and retrieved.")
                safe_results.extend(
                    [sample.structured_content, saved.structured_content, run.structured_content]
                )
                _assert_secret_absent(temporary, safe_results, temporary.database.path)
                print(f"{label} LIVE TEST PASS")
        finally:
            await runtime.aclose()


async def run_remote_ngrok(settings: Settings, *, include_scopus: bool) -> None:
    if not settings.tunnel.configured:
        raise ConfigError(
            'ngrok is not configured. Add authtoken = "..." under [tunnel] in the global config.'
        )
    if not settings.mcp_remote_auth.configured:
        raise ConfigError(
            'Remote MCP authentication is not configured. Add token = "..." under '
            "[mcp_remote_auth] in the global config."
        )
    if include_scopus and not settings.scopus.configured:
        raise ConfigError(
            'Scopus is not configured. Add api_key = "..." under [scopus] in the global config.'
        )
    with tempfile.TemporaryDirectory(prefix="research-gateway-remote-") as directory:
        temporary = settings.model_copy(deep=True)
        temporary.database.path = Path(directory) / "acceptance.db"
        temporary.service.host = "127.0.0.1"
        temporary.service.port = _free_port()
        temporary.tunnel.expose_ui = False
        runtime = GatewayRuntime.build(temporary)
        app = create_app(temporary, runtime)
        tunnel = NgrokTunnel(temporary, state_path=Path(directory) / "runtime" / "tunnel.json")
        async with _serve(app, temporary.service.port):
            public = await tunnel.astart()
            try:
                assert public.public_url and public.public_mcp_url
                await _wait_for_health(public.public_health_url or "")
                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
                    no_auth = await http.post(public.public_mcp_url, json={})
                    wrong_auth = await http.post(
                        public.public_mcp_url,
                        headers={"Authorization": "Bearer deliberately-wrong"},
                        json={},
                    )
                    hidden_ui = await http.get(f"{public.public_url}/ui")
                    hidden_api = await http.get(f"{public.public_url}/api/v1/status")
                if no_auth.status_code != 401 or wrong_auth.status_code != 401:
                    raise RuntimeError("Remote bearer rejection did not behave as required.")
                if hidden_ui.status_code != 404 or hidden_api.status_code != 404:
                    raise RuntimeError("The remote tunnel exposed the local UI or API.")
                headers = {
                    "Authorization": (
                        "Bearer " + temporary.mcp_remote_auth.token.get_secret_value()
                    )
                }
                async with httpx2.AsyncClient(headers=headers) as mcp_http:
                    transport = streamable_http_client(public.public_mcp_url, http_client=mcp_http)
                    async with Client(transport) as client:
                        tools = await client.list_tools()
                        if not any(tool.name == "gateway_status" for tool in tools.tools):
                            raise RuntimeError("Remote MCP did not expose gateway_status.")
                        status = await client.call_tool("gateway_status")
                        safe_results: list[object] = [status.structured_content]
                        if include_scopus:
                            safe_results.extend(await _remote_scopus_flow(client))
                        _assert_secret_absent(temporary, safe_results, temporary.database.path)
                if include_scopus:
                    print("LIVE SCOPUS NGROK ACCEPTANCE: PASS")
                else:
                    print("REMOTE NGROK MCP ACCEPTANCE: PASS")
            finally:
                await tunnel.astop()


async def _remote_scopus_flow(client: Client) -> list[object]:
    count = await client.call_tool("scopus_count", {"provider_query": SCOPUS_ACCEPTANCE_QUERY})
    sample = await client.call_tool(
        "scopus_search", {"provider_query": SCOPUS_ACCEPTANCE_QUERY, "limit": 3}
    )
    await client.call_tool(
        "study_create",
        {"study_id": "remote-scopus", "name": "Remote Scopus acceptance", "system_test": True},
    )
    await client.call_tool(
        "topic_create",
        {
            "study_id": "remote-scopus",
            "topic_id": "instruction-fine-tuning",
            "name": "Instruction fine-tuning",
        },
    )
    saved = await client.call_tool(
        "research_save_search",
        {
            "study_id": "remote-scopus",
            "topic_id": "instruction-fine-tuning",
            "provider": "scopus",
            "search_intent": "Validate remote Scopus capture",
            "provider_query": SCOPUS_ACCEPTANCE_QUERY,
            "max_records": 3,
        },
    )
    run = await client.call_tool(
        "search_run_get", {"search_run_id": saved.structured_content["search_run_id"]}
    )
    evidence = await client.call_tool("evidence_search", {"study_id": "remote-scopus", "limit": 10})
    if count.is_error or sample.is_error or saved.is_error or not run.structured_content["hits"]:
        raise RuntimeError("Remote Scopus MCP flow did not complete.")
    return [
        count.structured_content,
        sample.structured_content,
        saved.structured_content,
        run.structured_content,
        evidence.structured_content,
    ]


@asynccontextmanager
async def _serve(app: object, port: int) -> AsyncIterator[None]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.05)
        if not server.started:
            raise RuntimeError("Local acceptance server did not start.")
        yield
    finally:
        server.should_exit = True
        await task


async def _wait_for_health(url: str) -> None:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for _ in range(30):
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    raise RuntimeError("Public ngrok health endpoint did not become ready.")


def _assert_secret_absent(settings: Settings, results: list[object], database_path: Path) -> None:
    serialized = json.dumps(results, default=str)
    database_bytes = database_path.read_bytes() if database_path.is_file() else b""
    for secret in settings.secret_values():
        if secret and (secret in serialized or secret.encode() in database_bytes):
            raise RuntimeError(
                "A configured secret appeared in acceptance output or database data."
            )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])

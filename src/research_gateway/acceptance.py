from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import socket
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

import httpx
import httpx2
import uvicorn
from fastapi import Request
from fastapi.responses import HTMLResponse
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from research_gateway.api.app import create_app
from research_gateway.config import ConfigError, Settings
from research_gateway.mcp.server import create_mcp_server
from research_gateway.oauth.client import oauth_mcp_client
from research_gateway.oauth.security import hash_password
from research_gateway.oauth.setup import with_oauth_urls
from research_gateway.operations.logging import install_safe_request_target_filters
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
    if (
        settings.mcp_remote_auth.mode != "static_bearer"
        or not settings.mcp_remote_auth.token.get_secret_value()
    ):
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


async def run_oauth_ngrok(settings: Settings, *, include_scopus: bool) -> None:
    """Exercise the public ngrok surface through the official MCP OAuth client."""
    if not settings.tunnel.configured:
        raise ConfigError(
            'ngrok is not configured. Add authtoken = "..." under [tunnel] in the global config.'
        )
    if include_scopus and not settings.scopus.configured:
        raise ConfigError(
            'Scopus is not configured. Add api_key = "..." under [scopus] in the global config.'
        )
    with tempfile.TemporaryDirectory(prefix="research-gateway-oauth-") as directory:
        root = Path(directory)
        temporary, password = _temporary_oauth_settings(settings, root)
        tunnel = NgrokTunnel(temporary, state_path=root / "tunnel.json")
        public = await tunnel.astart()
        try:
            if not public.public_url or not public.public_mcp_url:
                raise RuntimeError("ngrok did not provide a public MCP URL.")
            runtime_settings = with_oauth_urls(temporary, public.public_url)
            runtime = GatewayRuntime.build(runtime_settings)
            app = create_app(runtime_settings, runtime)
            async with _serve(app, runtime_settings.service.port):
                await _wait_for_health(public.public_health_url or "")
                async with httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
                    unauthorized = await http.post(public.public_mcp_url, json={})
                    resource = await http.get(
                        f"{public.public_url}/.well-known/oauth-protected-resource/mcp"
                    )
                    metadata = await http.get(
                        f"{public.public_url}/.well-known/oauth-authorization-server"
                    )
                    hidden_ui = await http.get(f"{public.public_url}/ui")
                    hidden_api = await http.get(f"{public.public_url}/api/v1/status")
                challenge = unauthorized.headers.get("www-authenticate", "")
                if unauthorized.status_code != 401 or "resource_metadata=" not in challenge:
                    raise RuntimeError("Public MCP did not advertise OAuth resource metadata.")
                if resource.status_code != 200 or metadata.status_code != 200:
                    raise RuntimeError("Public OAuth discovery metadata was unavailable.")
                if hidden_ui.status_code != 404 or hidden_api.status_code != 404:
                    raise RuntimeError("OAuth exposure made the local UI or API public.")

                async with oauth_mcp_client(public.public_mcp_url, password=password) as (
                    client,
                    token_storage,
                ):
                    tools = await client.list_tools()
                    if not any(tool.name == "gateway_status" for tool in tools.tools):
                        raise RuntimeError("OAuth MCP did not expose gateway_status.")
                    status = await client.call_tool("gateway_status")
                    safe_results: list[object] = [_tool_payload(status)]
                    if include_scopus:
                        safe_results.extend(await _remote_scopus_flow(client))
                    _assert_secret_absent(runtime_settings, safe_results, temporary.database.path)
                    tokens = token_storage.tokens
                    if not tokens or not tokens.refresh_token:
                        raise RuntimeError("OAuth flow did not issue access and refresh tokens.")
                    store_bytes = runtime_settings.mcp_oauth.store_path.read_bytes()
                    if (
                        tokens.access_token.encode() in store_bytes
                        or tokens.refresh_token.encode() in store_bytes
                    ):
                        raise RuntimeError("Raw OAuth tokens leaked into the OAuth state store.")
                if include_scopus:
                    print("OAUTH SCOPUS NGROK ACCEPTANCE: PASS")
                else:
                    print("OAUTH NGROK MCP ACCEPTANCE: PASS")
        finally:
            await tunnel.astop()


async def run_oauth_browser_ngrok(settings: Settings) -> None:
    """Drive the real public OAuth approval flow with Playwright Chromium."""
    if not settings.tunnel.configured:
        raise ConfigError(
            'ngrok is not configured. Add authtoken = "..." under [tunnel] in the global config.'
        )
    script = Path(__file__).parents[2] / "ui" / "scripts" / "oauth-browser-ngrok.mjs"
    playwright = Path(__file__).parents[2] / "ui" / "node_modules" / "playwright"
    if not script.is_file() or not playwright.exists():
        raise ConfigError(
            "Playwright is not installed. Run npm ci and npx playwright install chromium "
            "in the ui directory."
        )

    with tempfile.TemporaryDirectory(prefix="research-gateway-oauth-browser-") as directory:
        root = Path(directory)
        temporary, password = _temporary_oauth_settings(settings, root)
        callback_path = "/oauth/acceptance-callback"
        callback_result: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()

        tunnel = NgrokTunnel(temporary, state_path=root / "tunnel.json")
        public = await tunnel.astart()
        try:
            if not public.public_url or not public.public_mcp_url:
                raise RuntimeError("ngrok did not provide a public MCP URL.")
            callback_origin = public.public_url
            callback_url = f"{callback_origin}{callback_path}"
            runtime_settings = with_oauth_urls(temporary, public.public_url)
            runtime = GatewayRuntime.build(runtime_settings)
            gateway_app = create_app(runtime_settings, runtime)

            async def oauth_callback(request: Request) -> HTMLResponse:
                values = {key: value for key, value in request.query_params.items()}
                if not callback_result.done():
                    callback_result.set_result(values)
                return HTMLResponse(
                    "<!doctype html><html><body>"
                    "<p data-oauth-callback='received'>OAuth callback received.</p>"
                    "</body></html>"
                )

            gateway_app.add_api_route(callback_path, oauth_callback, methods=["GET"])
            callback_route = gateway_app.router.routes.pop()
            mcp_mount_index = next(
                index
                for index, route in enumerate(gateway_app.router.routes)
                if getattr(route, "name", None) == "mcp"
            )
            gateway_app.router.routes.insert(mcp_mount_index, callback_route)

            async with _serve(gateway_app, runtime_settings.service.port):
                await _wait_for_health(public.public_health_url or "")
                verifier = secrets.token_urlsafe(64)
                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                    .decode()
                    .rstrip("=")
                )
                state = secrets.token_urlsafe(32)
                async with httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
                    registration = await http.post(
                        f"{public.public_url}/register",
                        json={
                            "client_name": "Research Gateway browser acceptance",
                            "redirect_uris": [callback_url],
                            "grant_types": ["authorization_code", "refresh_token"],
                            "response_types": ["code"],
                            "scope": runtime_settings.mcp_oauth.scope,
                            "token_endpoint_auth_method": "none",
                            "application_type": "native",
                        },
                    )
                    if registration.status_code != 201:
                        raise RuntimeError("OAuth browser client registration failed.")
                    client_id = str(registration.json()["client_id"])
                    authorization_url = f"{public.public_url}/authorize?" + urlencode(
                        {
                            "client_id": client_id,
                            "redirect_uri": callback_url,
                            "response_type": "code",
                            "code_challenge": challenge,
                            "code_challenge_method": "S256",
                            "state": state,
                            "scope": runtime_settings.mcp_oauth.scope,
                            "resource": public.public_mcp_url,
                        }
                    )
                    browser_result = await _run_playwright_browser(
                        script,
                        {
                            "authorization_url": authorization_url,
                            "password": password,
                            "gateway_origin": public.public_url,
                            "callback_origin": callback_origin,
                            "callback_path": callback_path,
                        },
                    )
                    callback = await asyncio.wait_for(callback_result, timeout=10)
                    if callback.get("state") != state or not callback.get("code"):
                        raise RuntimeError("OAuth browser callback state validation failed.")
                    token = await http.post(
                        f"{public.public_url}/token",
                        data={
                            "grant_type": "authorization_code",
                            "client_id": client_id,
                            "code": callback["code"],
                            "code_verifier": verifier,
                            "redirect_uri": callback_url,
                            "resource": public.public_mcp_url,
                        },
                    )
                if token.status_code != 200:
                    raise RuntimeError("OAuth browser authorization code exchange failed.")
                tokens = token.json()
                if not tokens.get("access_token") or not tokens.get("refresh_token"):
                    raise RuntimeError(
                        "OAuth browser flow did not issue access and refresh tokens."
                    )
                headers = {"Authorization": f"Bearer {tokens['access_token']}"}
                async with httpx2.AsyncClient(headers=headers) as mcp_http:
                    transport = streamable_http_client(public.public_mcp_url, http_client=mcp_http)
                    async with Client(transport) as client:
                        status = await client.call_tool("gateway_status")
                        if status.is_error:
                            raise RuntimeError("OAuth browser MCP gateway_status failed.")
                events = browser_result.get("events")
                if not isinstance(events, list):
                    raise RuntimeError("Playwright did not return safe browser network events.")
                approval_responses = [
                    event
                    for event in events
                    if isinstance(event, dict)
                    and event.get("method") == "POST"
                    and event.get("path") == "/oauth/authorize"
                ]
                if not approval_responses or any(
                    event.get("status") != 302 for event in approval_responses
                ):
                    raise RuntimeError("Browser approval POST did not return a safe redirect.")
                for event in events:
                    if isinstance(event, dict):
                        print(
                            "Browser network: "
                            f"{event.get('method')} {event.get('path')} -> {event.get('status')}"
                        )
                store_bytes = runtime_settings.mcp_oauth.store_path.read_bytes()
                for raw_secret in (
                    password,
                    verifier,
                    callback["code"],
                    tokens["access_token"],
                    tokens["refresh_token"],
                ):
                    if raw_secret.encode() in store_bytes:
                        raise RuntimeError("A raw OAuth browser secret leaked into state storage.")
                print("OAUTH BROWSER NGROK ACCEPTANCE: PASS")
        finally:
            await tunnel.astop()


async def _run_playwright_browser(script: Path, payload: dict[str, str]) -> dict[str, object]:
    """Run the browser harness without placing OAuth values in argv or errors."""
    try:
        process = await asyncio.create_subprocess_exec(
            "node",
            str(script),
            cwd=script.parents[1],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise ConfigError("Node.js is required for the Playwright browser acceptance.") from error
    stdout, _stderr = await process.communicate(json.dumps(payload).encode())
    try:
        result = json.loads(stdout.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Playwright browser acceptance returned an invalid result.") from error
    if not isinstance(result, dict):
        raise RuntimeError("Playwright browser acceptance returned an invalid result.")
    if process.returncode or result.get("ok") is not True:
        phase = result.get("phase")
        known_phases = {
            "launch",
            "authorization-navigation",
            "approval-form",
            "approval-submit",
        }
        safe_phase = phase if phase in known_phases else "unknown"
        safe_events = []
        for event in result.get("events", []):
            if isinstance(event, dict):
                safe_events.append(
                    f"{event.get('method')} {event.get('path')} -> {event.get('status')}"
                )
        location = result.get("location")
        safe_location = ""
        if isinstance(location, dict):
            safe_location = f" Last page: {location.get('origin')}{location.get('path')}."
        safe_trace = "; ".join(safe_events[-8:]) or "no browser responses"
        error_kind = result.get("error_kind")
        safe_error_kind = (
            error_kind
            if error_kind in {"ERR_CONNECTION_REFUSED", "ERR_FAILED", "ERR_ABORTED", "TimeoutError"}
            else "unknown"
        )
        raise RuntimeError(
            f"Playwright browser flow failed during {safe_phase} ({safe_error_kind})."
            f"{safe_location} "
            f"Safe network trace: {safe_trace}."
        )
    if result.get("allow_clicks") != 1:
        raise RuntimeError("Playwright did not perform exactly one approval click.")
    return result


def _temporary_oauth_settings(settings: Settings, root: Path) -> tuple[Settings, str]:
    """Build isolated OAuth acceptance settings while retaining external credentials."""
    install_safe_request_target_filters()
    password = secrets.token_urlsafe(24)
    temporary = settings.model_copy(deep=True)
    temporary.database.path = root / "acceptance.db"
    temporary.service.host = "127.0.0.1"
    temporary.service.port = _free_port()
    temporary.tunnel.domain = ""
    temporary.tunnel.expose_ui = False
    temporary.mcp_remote_auth.mode = "oauth"
    temporary.mcp_remote_auth.allow_unauthenticated = False
    temporary.mcp_oauth.enabled = True
    temporary.mcp_oauth.issuer_url = ""
    temporary.mcp_oauth.resource_url = ""
    temporary.mcp_oauth.scope = "research-gateway"
    temporary.mcp_oauth.admin_password_hash = type(temporary.mcp_oauth.admin_password_hash)(
        hash_password(password)
    )
    temporary.mcp_oauth.signing_secret = type(temporary.mcp_oauth.signing_secret)(
        secrets.token_urlsafe(48)
    )
    temporary.mcp_oauth.sealing_secret = type(temporary.mcp_oauth.sealing_secret)(
        secrets.token_urlsafe(48)
    )
    temporary.mcp_oauth.store_path = root / "oauth.sqlite3"
    return temporary, password


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
    saved_payload = _tool_payload(saved)
    run = await client.call_tool(
        "search_run_get", {"search_run_id": saved_payload["search_run_id"]}
    )
    evidence = await client.call_tool("evidence_search", {"study_id": "remote-scopus", "limit": 10})
    count_payload = _tool_payload(count)
    sample_payload = _tool_payload(sample)
    run_payload = _tool_payload(run)
    evidence_payload = _tool_payload(evidence)
    if count.is_error or sample.is_error or saved.is_error or not run_payload["hits"]:
        raise RuntimeError("Remote Scopus MCP flow did not complete.")
    return [
        count_payload,
        sample_payload,
        saved_payload,
        run_payload,
        evidence_payload,
    ]


def _tool_payload(result: object) -> dict[str, object]:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", None) or []
    if content and isinstance(getattr(content[0], "text", None), str):
        payload = json.loads(content[0].text)
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("MCP tool did not return an object result.")


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

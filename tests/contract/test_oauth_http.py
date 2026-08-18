from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import sqlite3
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from research_gateway.acceptance import _free_port, _serve
from research_gateway.api.app import create_app
from research_gateway.config import Settings
from research_gateway.db.database import EvidenceDatabase
from research_gateway.oauth.client import oauth_mcp_client
from research_gateway.oauth.security import hash_password, keyed_digest
from research_gateway.operations.backups import ExcelBackupService
from research_gateway.operations.logging import configure_logging
from research_gateway.runtime import GatewayRuntime
from research_gateway.sources.scopus import ScopusAdapter

PASSWORD = "correct horse battery staple"
VERIFIER = "v" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
)
REMOTE = {
    "Host": "127.0.0.1:8765",
    "X-Forwarded-For": "203.0.113.9",
    "X-Forwarded-Host": "gateway.example",
}


def _settings(tmp_path: Path, *, access_token_minutes: int = 60) -> Settings:
    return Settings.model_validate(
        {
            "database": {"path": tmp_path / "gateway.db"},
            "runtime": {"directory": tmp_path / "runtime"},
            "mcp_remote_auth": {"mode": "oauth"},
            "mcp_oauth": {
                "enabled": True,
                "issuer_url": "https://gateway.example",
                "resource_url": "https://gateway.example/mcp",
                "scope": "research-gateway",
                "admin_password_hash": hash_password(PASSWORD),
                "signing_secret": "s" * 64,
                "sealing_secret": "c" * 64,
                "store_path": tmp_path / "runtime" / "oauth.sqlite3",
                "access_token_minutes": access_token_minutes,
                "refresh_token_days": 30,
            },
            "acl_anthology": {"index_path": tmp_path / "missing.json"},
        }
    )


def _register(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/register",
        headers=REMOTE,
        json={
            "client_name": "ChatGPT",
            "redirect_uris": ["https://chatgpt.com/connector/oauth/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "research-gateway",
            "token_endpoint_auth_method": "none",
            "application_type": "web",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _begin_authorization(client: TestClient, client_id: str, **overrides: str):
    params = {
        "client_id": client_id,
        "redirect_uri": "https://chatgpt.com/connector/oauth/callback",
        "response_type": "code",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "chatgpt-state",
        "scope": "research-gateway",
        "resource": "https://gateway.example/mcp",
        **overrides,
    }
    response = client.get("/authorize", headers=REMOTE, params=params, follow_redirects=False)
    return response


def _approval_fields(response_text: str) -> tuple[str, str]:
    request_id = re.search(r'name="request" value="([^"]+)"', response_text)
    csrf = re.search(r'name="csrf" value="([^"]+)"', response_text)
    assert request_id and csrf
    return request_id.group(1), csrf.group(1)


def _authorize(client: TestClient, client_id: str) -> str:
    start = _begin_authorization(client, client_id)
    assert start.status_code == 302
    assert start.headers["location"].startswith("https://gateway.example/oauth/authorize?")
    login = client.get(start.headers["location"], headers=REMOTE)
    assert login.status_code == 200
    assert "ChatGPT is requesting access to Research Gateway" in login.text
    assert "research-gateway" in login.text
    request_id, csrf = _approval_fields(login.text)

    wrong = client.post(
        "/oauth/authorize",
        headers=REMOTE,
        data={"request": request_id, "csrf": csrf, "password": "wrong", "decision": "allow"},
    )
    assert wrong.status_code == 401

    approved = client.post(
        "/oauth/authorize",
        headers=REMOTE,
        data={"request": request_id, "csrf": csrf, "password": PASSWORD, "decision": "allow"},
        follow_redirects=False,
    )
    assert approved.status_code == 302
    callback = urlparse(approved.headers["location"])
    assert f"{callback.scheme}://{callback.netloc}{callback.path}" == (
        "https://chatgpt.com/connector/oauth/callback"
    )
    values = parse_qs(callback.query)
    assert values["state"] == ["chatgpt-state"]
    return values["code"][0]


def _token(client: TestClient, client_id: str, code: str, verifier: str = VERIFIER):
    return client.post(
        "/token",
        headers=REMOTE,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": "https://chatgpt.com/connector/oauth/callback",
            "resource": "https://gateway.example/mcp",
        },
    )


def test_oauth_discovery_login_pkce_token_mcp_and_rotation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.logging.path = tmp_path / "gateway.log"
    settings.backup.directory = tmp_path / "backups"
    configure_logging(settings)
    with TestClient(create_app(settings), base_url="https://gateway.example") as client:
        unauthorized = client.post("/mcp", headers=REMOTE, json={})
        assert unauthorized.status_code == 401
        assert (
            'resource_metadata="https://gateway.example/.well-known/oauth-protected-resource/mcp"'
        ) in unauthorized.headers["www-authenticate"]

        resource = client.get("/.well-known/oauth-protected-resource/mcp", headers=REMOTE)
        assert resource.status_code == 200
        assert resource.json() == {
            "resource": "https://gateway.example/mcp",
            "authorization_servers": ["https://gateway.example"],
            "scopes_supported": ["research-gateway"],
            "bearer_methods_supported": ["header"],
        }
        metadata = client.get("/.well-known/oauth-authorization-server", headers=REMOTE)
        assert metadata.status_code == 200
        assert metadata.json()["issuer"] == "https://gateway.example"
        assert metadata.json()["registration_endpoint"] == "https://gateway.example/register"
        assert metadata.json()["code_challenge_methods_supported"] == ["S256"]
        assert "refresh_token" in metadata.json()["grant_types_supported"]

        registration = _register(client)
        client_id = str(registration["client_id"])
        code = _authorize(client, client_id)

        bad_pkce = _token(client, client_id, code, verifier="x" * 64)
        assert bad_pkce.status_code == 400
        assert bad_pkce.json()["error"] == "invalid_grant"

        token_response = _token(client, client_id, code)
        assert token_response.status_code == 200, token_response.text
        tokens = token_response.json()
        assert tokens["token_type"] == "Bearer"
        assert tokens["access_token"]
        assert tokens["refresh_token"]

        replay = _token(client, client_id, code)
        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_grant"

        invalid = client.post(
            "/mcp", headers={**REMOTE, "Authorization": "Bearer invalid"}, json={}
        )
        assert invalid.status_code == 401

        mcp = client.post(
            "/mcp",
            headers={
                **REMOTE,
                "Authorization": f"Bearer {tokens['access_token']}",
                "MCP-Protocol-Version": "2026-07-28",
                "MCP-Method": "tools/list",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
        )
        assert mcp.status_code == 200, mcp.text
        assert any(tool["name"] == "gateway_status" for tool in mcp.json()["result"]["tools"])

        refreshed = client.post(
            "/token",
            headers=REMOTE,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
                "scope": "research-gateway",
                "resource": "https://gateway.example/mcp",
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        replacement = refreshed.json()
        assert replacement["access_token"] != tokens["access_token"]
        assert replacement["refresh_token"] != tokens["refresh_token"]
        reused = client.post(
            "/token",
            headers=REMOTE,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
            },
        )
        assert reused.status_code == 400
        assert reused.json()["error"] == "invalid_grant"

        assert client.get("/ui", headers=REMOTE).status_code == 404
        assert client.get("/api/v1/status", headers=REMOTE).status_code == 404

    logging.getLogger("research_gateway.oauth.test").info("OAuth flow completed safely.")
    backup = asyncio.run(
        ExcelBackupService(
            EvidenceDatabase(settings.database.path),
            settings.backup.directory,
        ).create()
    )
    oauth_bytes = settings.mcp_oauth.store_path.read_bytes()
    log_bytes = settings.logging.path.read_bytes()
    with zipfile.ZipFile(backup.path) as workbook:
        workbook_bytes = b"".join(workbook.read(name) for name in workbook.namelist())
    for secret in (
        PASSWORD,
        settings.mcp_oauth.admin_password_hash.get_secret_value(),
        settings.mcp_oauth.signing_secret.get_secret_value(),
        settings.mcp_oauth.sealing_secret.get_secret_value(),
        tokens["access_token"],
        tokens["refresh_token"],
        replacement["access_token"],
        replacement["refresh_token"],
    ):
        assert secret.encode() not in oauth_bytes
        assert secret.encode() not in log_bytes
        assert secret.encode() not in workbook_bytes
    evidence_bytes = settings.database.path.read_bytes()
    assert tokens["access_token"].encode() not in evidence_bytes
    assert tokens["refresh_token"].encode() not in evidence_bytes


def test_oauth_rejects_bad_redirect_resource_state_and_reused_approval(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path)), base_url="https://gateway.example") as client:
        registration = _register(client)
        client_id = str(registration["client_id"])
        bad_redirect = _begin_authorization(
            client, client_id, redirect_uri="https://attacker.example/callback"
        )
        assert bad_redirect.status_code == 400
        bad_resource = _begin_authorization(
            client, client_id, resource="https://attacker.example/mcp"
        )
        assert bad_resource.status_code == 302
        assert "error=invalid_target" in bad_resource.headers["location"]

        start = _begin_authorization(client, client_id)
        login = client.get(start.headers["location"], headers=REMOTE)
        request_id, csrf = _approval_fields(login.text)
        bad_csrf = client.post(
            "/oauth/authorize",
            headers=REMOTE,
            data={"request": request_id, "csrf": "bad", "password": PASSWORD, "decision": "allow"},
        )
        assert bad_csrf.status_code == 400
        approved = client.post(
            "/oauth/authorize",
            headers=REMOTE,
            data={"request": request_id, "csrf": csrf, "password": PASSWORD, "decision": "allow"},
            follow_redirects=False,
        )
        assert approved.status_code == 302
        replay = client.post(
            "/oauth/authorize",
            headers=REMOTE,
            data={"request": request_id, "csrf": csrf, "password": PASSWORD, "decision": "allow"},
        )
        assert replay.status_code == 400


def test_expired_authorization_code_and_access_token_are_rejected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    signing = settings.mcp_oauth.signing_secret.get_secret_value()
    with TestClient(create_app(settings), base_url="https://gateway.example") as client:
        registration = _register(client)
        client_id = str(registration["client_id"])
        expired_code = _authorize(client, client_id)
        with sqlite3.connect(settings.mcp_oauth.store_path) as connection:
            connection.execute(
                "UPDATE oauth_records SET expires_at = 0 WHERE kind = 'code' AND digest = ?",
                (keyed_digest(signing, expired_code),),
            )
        rejected_code = _token(client, client_id, expired_code)
        assert rejected_code.status_code == 400
        assert rejected_code.json()["error"] == "invalid_grant"

        code = _authorize(client, client_id)
        tokens = _token(client, client_id, code).json()
        with sqlite3.connect(settings.mcp_oauth.store_path) as connection:
            connection.execute(
                "UPDATE oauth_records SET expires_at = 0 WHERE kind = 'access' AND digest = ?",
                (keyed_digest(signing, tokens["access_token"]),),
            )
        expired_access = client.post(
            "/mcp",
            headers={**REMOTE, "Authorization": f"Bearer {tokens['access_token']}"},
            json={},
        )
        assert expired_access.status_code == 401


@pytest.mark.asyncio
async def test_official_mcp_oauth_client_full_contract_with_scopus_fixture(
    tmp_path: Path,
) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    settings = _settings(tmp_path)
    settings.service.port = port
    settings.mcp_oauth.issuer_url = base_url
    settings.mcp_oauth.resource_url = f"{base_url}/mcp"
    settings.scopus.api_key = SecretStr("fixture-scopus-key")
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "scopus_search.json").read_text(encoding="utf-8")
    )
    scopus = ScopusAdapter(
        settings.scopus,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        ),
    )
    runtime = GatewayRuntime.build(settings)
    runtime.sources.add(scopus)
    app = create_app(settings, runtime)
    forwarded = {
        "X-Forwarded-For": "203.0.113.9",
        "X-Forwarded-Host": "gateway.example",
    }

    async with (
        _serve(app, port),
        oauth_mcp_client(f"{base_url}/mcp", password=PASSWORD, request_headers=forwarded) as (
            client,
            storage,
        ),
    ):
        tools = await client.list_tools()
        assert any(tool.name == "gateway_status" for tool in tools.tools)
        status = await client.call_tool("gateway_status")
        count = await client.call_tool(
            "scopus_count", {"provider_query": 'TITLE-ABS-KEY("fine tuning")'}
        )
        assert not status.is_error, repr(status)
        assert not count.is_error, repr(count)
        status_payload = status.structured_content or json.loads(status.content[0].text)
        count_payload = count.structured_content or json.loads(count.content[0].text)
        assert status_payload["status"] == "ok"
        assert count_payload["total"] == 42
        assert storage.tokens is not None
        assert storage.tokens.access_token
        assert storage.tokens.refresh_token

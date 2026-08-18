from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import httpx2
from mcp.client import Client
from mcp.client.auth.oauth2 import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)


class MemoryTokenStorage(TokenStorage):
    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


class _ApprovalFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "input":
            return
        values = dict(attrs)
        name = values.get("name")
        value = values.get("value")
        if name and value is not None:
            self.fields[name] = value


@asynccontextmanager
async def oauth_mcp_client(
    server_url: str,
    *,
    password: str,
    request_headers: dict[str, str] | None = None,
) -> AsyncIterator[tuple[Client, MemoryTokenStorage]]:
    """Connect through the official MCP OAuth client and approve locally in-process."""
    storage = MemoryTokenStorage()
    callback: AuthorizationCodeResult | None = None
    headers = request_headers or {}

    async def redirect_handler(authorization_url: str) -> None:
        nonlocal callback
        async with httpx.AsyncClient(
            headers=headers, timeout=30, follow_redirects=False
        ) as browser:
            authorize = await browser.get(authorization_url)
            if authorize.status_code != 302:
                raise RuntimeError("OAuth authorization endpoint did not redirect to approval.")
            approval_url = urljoin(authorization_url, authorize.headers["location"])
            approval = await browser.get(approval_url)
            if approval.status_code != 200:
                raise RuntimeError("OAuth approval page was not available.")
            parser = _ApprovalFormParser()
            parser.feed(approval.text)
            approved = await browser.post(
                urljoin(approval_url, "/oauth/authorize"),
                data={
                    "request": parser.fields.get("request", ""),
                    "csrf": parser.fields.get("csrf", ""),
                    "password": password,
                    "decision": "allow",
                },
            )
            if approved.status_code != 302:
                raise RuntimeError("OAuth approval was rejected.")
            values = parse_qs(urlparse(approved.headers["location"]).query)
            callback = AuthorizationCodeResult(
                code=values["code"][0],
                state=(values.get("state") or [None])[0],
                iss=(values.get("iss") or [None])[0],
            )

    async def callback_handler() -> AuthorizationCodeResult:
        if callback is None:
            raise RuntimeError("OAuth callback was not produced.")
        return callback

    metadata = OAuthClientMetadata(
        client_name="Research Gateway OAuth acceptance",
        redirect_uris=["http://127.0.0.1:31119/oauth/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="research-gateway",
        token_endpoint_auth_method="none",
    )
    auth = OAuthClientProvider(
        server_url=server_url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    async with httpx2.AsyncClient(auth=auth, headers=headers) as authenticated_http:
        transport = streamable_http_client(server_url, http_client=authenticated_http)
        async with Client(transport) as client:
            yield client, storage

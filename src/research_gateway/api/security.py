from __future__ import annotations

import hmac

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from research_gateway.config import McpRemoteAuthSettings


class RemoteSurfaceMiddleware:
    """Keep UI/API loopback-only and require a bearer token for remote MCP."""

    def __init__(self, app: ASGIApp, auth: McpRemoteAuthSettings, expose_ui: bool = False) -> None:
        self.app = app
        self.auth = auth
        self.expose_ui = expose_ui

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        remote = _is_remote(headers)
        path = scope.get("path", "")
        if (
            remote
            and not self.expose_ui
            and path != "/health"
            and not (path == "/mcp" or path.startswith("/mcp/"))
        ):
            await JSONResponse({"detail": "Not found"}, status_code=404)(scope, receive, send)
            return
        if remote and (path == "/mcp" or path.startswith("/mcp/")):
            if self.auth.allow_unauthenticated:
                await self.app(scope, receive, send)
                return
            expected = self.auth.token.get_secret_value()
            if not expected:
                await JSONResponse(
                    {"detail": "Remote MCP authentication is not configured"}, status_code=503
                )(scope, receive, send)
                return
            supplied = headers.get("authorization", "")
            valid = supplied.startswith("Bearer ") and hmac.compare_digest(
                supplied.removeprefix("Bearer "), expected
            )
            if not valid:
                await JSONResponse(
                    {"detail": "Bearer authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _is_remote(headers: Headers) -> bool:
    if headers.get("x-forwarded-for") or headers.get("x-forwarded-host"):
        return True
    host = headers.get("host", "").rsplit(":", 1)[0].strip("[]").casefold()
    return host not in {"127.0.0.1", "localhost", "::1", "testserver"}

from __future__ import annotations

import hmac

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from research_gateway.oauth.provider import SingleUserOAuthProvider

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_CSRF_COOKIE = "rg_oauth_csrf"


def install_approval_routes(server: MCPServer[None], provider: SingleUserOAuthProvider) -> None:
    @server.custom_route("/oauth/authorize", methods=["GET"])
    async def approval_page(request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        try:
            content, csrf = provider.approval_page(request_id)
        except ValueError:
            return PlainTextResponse(
                "Authorization request is invalid or expired.",
                status_code=400,
                headers=_SECURITY_HEADERS,
            )
        response = HTMLResponse(content, headers=_SECURITY_HEADERS)
        response.set_cookie(
            _CSRF_COOKIE,
            csrf,
            httponly=True,
            secure=provider.settings.issuer_url.startswith("https://"),
            samesite="strict",
            max_age=provider.settings.approval_request_seconds,
            path="/oauth/authorize",
        )
        return response

    @server.custom_route("/oauth/authorize", methods=["POST"])
    async def approval_submit(request: Request) -> Response:
        form = await request.form()
        field_names = ("request", "csrf", "password", "decision")
        fields = {
            name: value if isinstance(value, str) else ""
            for name, value in ((name, form.get(name)) for name in field_names)
        }
        cookie = request.cookies.get(_CSRF_COOKIE, "")
        if not cookie or not hmac.compare_digest(cookie, fields["csrf"]):
            return PlainTextResponse(
                "Authorization request validation failed.",
                status_code=400,
                headers=_SECURITY_HEADERS,
            )
        try:
            redirect = provider.approve(
                request_id=fields["request"],
                csrf=fields["csrf"],
                password=fields["password"],
                decision=fields["decision"],
            )
        except PermissionError:
            return PlainTextResponse(
                "Authorization password is incorrect.",
                status_code=401,
                headers=_SECURITY_HEADERS,
            )
        except ValueError:
            return PlainTextResponse(
                "Authorization request is invalid, expired, or already used.",
                status_code=400,
                headers=_SECURITY_HEADERS,
            )
        response = RedirectResponse(redirect, status_code=302, headers=_SECURITY_HEADERS)
        response.delete_cookie(_CSRF_COOKIE, path="/oauth/authorize")
        return response

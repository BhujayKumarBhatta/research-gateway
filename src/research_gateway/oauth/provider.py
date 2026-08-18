from __future__ import annotations

import html
import ipaddress
import logging
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from research_gateway.config import McpOAuthSettings
from research_gateway.oauth.security import csrf_value, keyed_digest, verify_password
from research_gateway.oauth.store import OAuthStore

logger = logging.getLogger(__name__)


_DNS_HOST = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)


@dataclass(frozen=True)
class ApprovalPage:
    content: str
    csrf: str
    callback_origin: str


@dataclass(frozen=True)
class ApprovalResult:
    redirect_url: str
    callback_origin: str
    correlation_id: str
    replayed: bool


def validated_redirect_origin(redirect_uri: str) -> str:
    """Return one CSP-safe origin for an allowed registered OAuth redirect URI."""
    try:
        parsed = urlparse(redirect_uri)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("OAuth redirect URI is malformed.") from error
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("OAuth redirect URI cannot produce a safe callback origin.")

    scheme = parsed.scheme.casefold()
    host = hostname.casefold()
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if scheme != "https" and not (scheme == "http" and loopback):
        raise ValueError("OAuth redirect URI must use HTTPS or loopback HTTP.")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            serialized_host = host.encode("idna").decode("ascii")
        except UnicodeError as error:
            raise ValueError("OAuth redirect URI has an invalid host.") from error
        if not _DNS_HOST.fullmatch(serialized_host):
            raise ValueError("OAuth redirect URI has an invalid host.") from None
    else:
        if getattr(address, "scope_id", None):
            raise ValueError("OAuth redirect URI cannot use an IPv6 scope identifier.")
        serialized_host = f"[{address.compressed}]" if address.version == 6 else address.compressed

    authority = serialized_host if port is None else f"{serialized_host}:{port}"
    return f"{scheme}://{authority}"


class SingleUserOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """MCP SDK provider for one locally authenticated Research Gateway owner."""

    def __init__(self, settings: McpOAuthSettings, store_path: Path) -> None:
        self.settings = settings
        self.store = OAuthStore(store_path)
        self._signing_secret = settings.signing_secret.get_secret_value()
        self._sealing_secret = settings.sealing_secret.get_secret_value()
        self.local_transport_token = secrets.token_urlsafe(48)

    def _digest(self, value: str) -> str:
        return keyed_digest(self._signing_secret, value)

    def correlation_id(self, request_id: str) -> str:
        """Return a safe short flow identifier without revealing the request ID."""
        return keyed_digest(self._signing_secret, f"oauth-flow:{request_id}")[:12]

    def _authorization_code(self, request_id: str) -> str:
        return keyed_digest(self._sealing_secret, f"oauth-code:{request_id}")

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        payload = self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate(payload) if payload else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.redirect_uris:
            raise RegistrationError("invalid_redirect_uri", "At least one redirect URI is required")
        for redirect in client_info.redirect_uris:
            try:
                validated_redirect_origin(str(redirect))
            except ValueError as error:
                raise RegistrationError(
                    "invalid_redirect_uri", "Redirect URIs must use HTTPS or loopback HTTP"
                ) from error
        # This single-user server registers public PKCE clients. Avoid retaining a
        # dynamically generated client secret that is unnecessary for this flow.
        client_info.token_endpoint_auth_method = "none"
        client_info.client_secret = None
        client_info.client_secret_expires_at = None
        requested = set((client_info.scope or "").split())
        if requested and requested != {self.settings.scope}:
            raise RegistrationError("invalid_client_metadata", "Unsupported OAuth scope")
        client_info.scope = self.settings.scope
        self.store.put_client(client_info.client_id, client_info.model_dump(mode="json"))

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource != self.settings.resource_url:
            raise AuthorizeError("invalid_target", "The requested resource is not this MCP server")
        scopes = params.scopes or [self.settings.scope]
        if scopes != [self.settings.scope]:
            raise AuthorizeError("invalid_scope", "Unsupported OAuth scope")
        request_id = secrets.token_urlsafe(32)
        expires_at = time.time() + self.settings.approval_request_seconds
        payload = {
            "client_id": client.client_id,
            "client_name": client.client_name or "ChatGPT",
            "state": params.state,
            "scopes": scopes,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
        }
        self.store.put_record("approval", self._digest(request_id), payload, expires_at)
        logger.info(
            "oauth authorize request created oauth_flow=%s", self.correlation_id(request_id)
        )
        return f"{self.settings.issuer_url.rstrip('/')}/oauth/authorize?request={quote(request_id)}"

    def approval_page(self, request_id: str) -> ApprovalPage:
        payload = self.store.get_record("approval", self._digest(request_id))
        if not payload:
            raise ValueError("Authorization request is invalid or expired.")
        callback_origin = validated_redirect_origin(str(payload["redirect_uri"]))
        client_name = html.escape(str(payload.get("client_name") or "ChatGPT"))
        scope = html.escape(" ".join(payload.get("scopes") or [self.settings.scope]))
        csrf = csrf_value(self._sealing_secret, request_id)
        logger.info("oauth approval page served oauth_flow=%s", self.correlation_id(request_id))
        return ApprovalPage(
            content=self._render_page(request_id, csrf, client_name, scope),
            csrf=csrf,
            callback_origin=callback_origin,
        )

    def approve(
        self,
        *,
        request_id: str,
        csrf: str,
        password: str,
        decision: str,
    ) -> ApprovalResult:
        expected_csrf = csrf_value(self._sealing_secret, request_id)
        if not secrets.compare_digest(csrf, expected_csrf):
            raise ValueError("Authorization request validation failed.")
        if not verify_password(password, self.settings.admin_password_hash.get_secret_value()):
            raise PermissionError("Authorization password is incorrect.")
        approval_digest = self._digest(request_id)
        correlation = self.correlation_id(request_id)
        payload = self.store.get_record("approval", approval_digest)
        if not payload:
            completed = self.store.get_record("approval_complete", approval_digest)
            if not completed:
                raise ValueError("Authorization request is invalid, expired, or already used.")
            callback_origin = validated_redirect_origin(str(completed["redirect_uri"]))
            logger.info("oauth approval duplicate/replay detected oauth_flow=%s", correlation)
            return ApprovalResult(
                redirect_url=self._completed_redirect(request_id, completed),
                callback_origin=callback_origin,
                correlation_id=correlation,
                replayed=True,
            )
        redirect_uri = str(payload["redirect_uri"])
        callback_origin = validated_redirect_origin(redirect_uri)
        state = payload.get("state")
        if decision != "allow":
            consumed = self.store.consume_record("approval", approval_digest)
            if not consumed:
                raise ValueError("Authorization request is invalid, expired, or already used.")
            return ApprovalResult(
                redirect_url=construct_redirect_uri(
                    redirect_uri,
                    error="access_denied",
                    error_description="Access was not approved",
                    state=state,
                ),
                callback_origin=callback_origin,
                correlation_id=correlation,
                replayed=False,
            )
        raw_code = self._authorization_code(request_id)
        code_expires_at = time.time() + self.settings.authorization_code_seconds
        code_payload = {
            "client_id": payload["client_id"],
            "scopes": payload["scopes"],
            "expires_at": code_expires_at,
            "code_challenge": payload["code_challenge"],
            "redirect_uri": redirect_uri,
            "redirect_uri_provided_explicitly": payload["redirect_uri_provided_explicitly"],
            "resource": payload["resource"],
            "subject": "research-gateway-owner",
            "_oauth_flow": correlation,
        }
        completion_payload = {
            "redirect_uri": redirect_uri,
            "state": state,
            "_oauth_flow": correlation,
        }
        completed = self.store.complete_approval(
            approval_digest,
            code_digest=self._digest(raw_code),
            code_payload=code_payload,
            code_expires_at=code_expires_at,
            completion_payload=completion_payload,
            completion_expires_at=time.time() + self.settings.approval_completion_seconds,
        )
        if not completed:
            raise ValueError("Authorization request is invalid, expired, or already used.")
        outcome, saved_completion = completed
        if outcome == "duplicate":
            logger.info("oauth approval duplicate/replay detected oauth_flow=%s", correlation)
        else:
            logger.info("oauth approval accepted oauth_flow=%s", correlation)
        return ApprovalResult(
            redirect_url=self._completed_redirect(request_id, saved_completion),
            callback_origin=validated_redirect_origin(str(saved_completion["redirect_uri"])),
            correlation_id=correlation,
            replayed=outcome == "duplicate",
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        payload = self.store.get_record("code", self._digest(authorization_code))
        if not payload or payload["client_id"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **self._public_payload(payload))

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        payload = self.store.consume_record("code", self._digest(authorization_code.code))
        if not payload or payload["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "Authorization code was already used")
        result = self._issue_tokens(
            client_id=client.client_id,
            scopes=list(payload["scopes"]),
            resource=str(payload["resource"]),
            subject=str(payload["subject"]),
        )
        logger.info("oauth token exchanged oauth_flow=%s", payload.get("_oauth_flow", "unknown"))
        return result

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        digest = self._digest(refresh_token)
        payload = self.store.get_record("refresh", digest)
        if not payload:
            inactive = self.store.get_record("refresh", digest, include_inactive=True)
            if inactive and inactive.get("_used_at") and inactive.get("_family_id"):
                self.store.revoke_family(str(inactive["_family_id"]))
            return None
        if payload["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, **self._token_payload(payload))

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        payload = self.store.consume_record("refresh", self._digest(refresh_token.token))
        if not payload or payload["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "Refresh token was already used")
        family_id = str(payload["_family_id"])
        self.store.revoke_family(family_id)
        return self._issue_tokens(
            client_id=client.client_id,
            scopes=scopes,
            resource=str(payload["resource"]),
            subject=str(payload["subject"]),
            family_id=family_id,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        if secrets.compare_digest(token, self.local_transport_token):
            return AccessToken(
                token=token,
                client_id="research-gateway-local",
                scopes=[self.settings.scope],
                resource=self.settings.resource_url,
                subject="research-gateway-owner",
            )
        payload = self.store.get_record("access", self._digest(token))
        if not payload or payload.get("resource") != self.settings.resource_url:
            return None
        return AccessToken(token=token, **self._token_payload(payload))

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        for kind in ("access", "refresh"):
            payload = self.store.get_record(kind, self._digest(token.token), include_inactive=True)
            if payload and payload.get("_family_id"):
                self.store.revoke_family(str(payload["_family_id"]))
                return

    def _issue_tokens(
        self,
        *,
        client_id: str,
        scopes: list[str],
        resource: str,
        subject: str,
        family_id: str | None = None,
    ) -> OAuthToken:
        if resource != self.settings.resource_url:
            raise TokenError("invalid_target", "The authorization is for another resource")
        selected_family = family_id or secrets.token_hex(16)
        access = secrets.token_urlsafe(48)
        refresh = secrets.token_urlsafe(48)
        now = int(time.time())
        access_expires = now + self.settings.access_token_minutes * 60
        refresh_expires = now + self.settings.refresh_token_days * 86400
        common = {
            "client_id": client_id,
            "scopes": scopes,
            "resource": resource,
            "subject": subject,
        }
        self.store.put_record(
            "access",
            self._digest(access),
            {**common, "expires_at": access_expires},
            access_expires,
            family_id=selected_family,
        )
        self.store.put_record(
            "refresh",
            self._digest(refresh),
            {**common, "expires_at": refresh_expires},
            refresh_expires,
            family_id=selected_family,
        )
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            token_type="Bearer",
            expires_in=self.settings.access_token_minutes * 60,
            scope=" ".join(scopes),
        )

    @staticmethod
    def _public_payload(payload: dict[str, object]) -> dict[str, object]:
        return {key: value for key, value in payload.items() if not key.startswith("_")}

    @staticmethod
    def _token_payload(payload: dict[str, object]) -> dict[str, object]:
        return {
            key: value
            for key, value in payload.items()
            if key in {"client_id", "scopes", "expires_at", "resource", "subject"}
        }

    def _completed_redirect(self, request_id: str, completion: dict[str, object]) -> str:
        return construct_redirect_uri(
            str(completion["redirect_uri"]),
            code=self._authorization_code(request_id),
            state=completion.get("state"),
        )

    @staticmethod
    def _render_page(request_id: str, csrf: str, client_name: str, scope: str) -> str:
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Research Gateway authorization</title>
<style>
body{{font:16px system-ui;max-width:34rem;margin:4rem auto;padding:0 1rem;color:#17202a}}
main{{border:1px solid #d5d8dc;border-radius:12px;padding:1.5rem}}
label{{display:block;margin:1rem 0 .4rem}}
input{{box-sizing:border-box;width:100%;padding:.7rem}}
button{{margin-top:1rem;padding:.7rem 1.2rem}}
</style></head>
<body><main><h1>Research Gateway</h1>
<p><strong>{client_name} is requesting access to Research Gateway</strong></p>
<p>Requested scope: <code>{scope}</code></p>
<form action="/oauth/authorize" method="post">
<input type="hidden" name="request" value="{html.escape(request_id, quote=True)}">
<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">
<label for="password">Research Gateway OAuth password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button name="decision" value="allow" type="submit">Allow</button>
<button name="decision" value="deny" type="submit">Deny</button>
</form></main></body></html>"""

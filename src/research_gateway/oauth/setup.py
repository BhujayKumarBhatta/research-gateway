from __future__ import annotations

import json
import os
import re
import secrets
import tomllib
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from research_gateway.config import ConfigError, Settings, load_settings
from research_gateway.oauth.security import hash_password
from research_gateway.oauth.store import OAuthStore


@dataclass(frozen=True)
class OAuthInitialization:
    config_path: Path
    store_path: Path
    generated_password: str | None


def initialize_oauth(
    config_path: Path,
    *,
    password: str | None = None,
    generate_password: bool = False,
) -> OAuthInitialization:
    """Initialize single-user OAuth without replacing unrelated configuration."""
    selected = config_path.expanduser().absolute()
    if not selected.is_file():
        raise ConfigError(f"Configuration file does not exist: {selected}")
    if password and generate_password:
        raise ValueError("Choose an interactive password or generated password, not both.")
    generated = secrets.token_urlsafe(24) if generate_password else None
    chosen = password or generated
    if not chosen:
        raise ValueError("An OAuth authorization password is required.")
    password_hash = hash_password(chosen)

    with selected.open("rb") as stream:
        raw = tomllib.load(stream)
    current = load_settings(selected, require_file=True)
    oauth_raw = raw.get("mcp_oauth") if isinstance(raw.get("mcp_oauth"), dict) else {}
    signing_secret = str(oauth_raw.get("signing_secret") or secrets.token_urlsafe(48))
    sealing_secret = str(oauth_raw.get("sealing_secret") or secrets.token_urlsafe(48))
    runtime_directory = current.runtime.directory or current.database.path.parent / "runtime"
    store_path = Path(oauth_raw.get("store_path") or runtime_directory / "oauth.sqlite3")
    store_path = store_path.expanduser().absolute()

    text = selected.read_text(encoding="utf-8")
    values = {
        ("mcp_remote_auth", "mode"): json.dumps("oauth"),
        ("mcp_remote_auth", "allow_unauthenticated"): "false",
        ("mcp_oauth", "enabled"): "true",
        ("mcp_oauth", "issuer_url"): json.dumps(str(oauth_raw.get("issuer_url") or "")),
        ("mcp_oauth", "resource_url"): json.dumps(str(oauth_raw.get("resource_url") or "")),
        ("mcp_oauth", "scope"): json.dumps(str(oauth_raw.get("scope") or "research-gateway")),
        ("mcp_oauth", "admin_password_hash"): json.dumps(password_hash),
        ("mcp_oauth", "signing_secret"): json.dumps(signing_secret),
        ("mcp_oauth", "sealing_secret"): json.dumps(sealing_secret),
        ("mcp_oauth", "store_path"): json.dumps(str(store_path)),
        ("mcp_oauth", "access_token_minutes"): str(
            int(oauth_raw.get("access_token_minutes") or 60)
        ),
        ("mcp_oauth", "refresh_token_days"): str(int(oauth_raw.get("refresh_token_days") or 30)),
    }
    for (section, key), serialized in values.items():
        text = _set_toml_value(text, section, key, serialized)

    temporary = selected.with_name(f".{selected.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        with temporary.open("rb") as stream:
            tomllib.load(stream)
        with suppress(OSError):
            temporary.chmod(0o600)
        os.replace(temporary, selected)
    finally:
        temporary.unlink(missing_ok=True)
    with suppress(OSError):
        selected.chmod(0o600)
    OAuthStore(store_path)
    load_settings(selected, require_file=True)
    return OAuthInitialization(selected, store_path, generated)


def with_oauth_urls(settings: Settings, base_url: str) -> Settings:
    """Fill blank OAuth URLs from the active ngrok or loopback service base."""
    if settings.mcp_remote_auth.mode != "oauth":
        return settings
    if not settings.mcp_oauth.configured:
        raise ConfigError("OAuth mode is selected but oauth-init has not been completed.")
    result = settings.model_copy(deep=True)
    selected_base = base_url.rstrip("/")
    result.mcp_oauth.issuer_url = result.mcp_oauth.issuer_url.rstrip("/") or selected_base
    result.mcp_oauth.resource_url = (
        result.mcp_oauth.resource_url.rstrip("/") or f"{selected_base}/mcp"
    )
    if result.mcp_oauth.resource_url != f"{result.mcp_oauth.issuer_url}/mcp":
        raise ConfigError("OAuth resource_url must be the issuer URL followed by /mcp.")
    return result


def _set_toml_value(text: str, section: str, key: str, serialized: str) -> str:
    lines = text.splitlines()
    header = f"[{section}]"
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == header)
    except StopIteration:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([header, f"{key} = {serialized}"])
        return "\n".join(lines) + "\n"
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.fullmatch(r"\s*\[[^]]+]\s*", lines[index])
        ),
        len(lines),
    )
    pattern = re.compile(rf"\s*{re.escape(key)}\s*=")
    for index in range(start + 1, end):
        if pattern.match(lines[index]):
            lines[index] = f"{key} = {serialized}"
            return "\n".join(lines) + "\n"
    lines.insert(end, f"{key} = {serialized}")
    return "\n".join(lines) + "\n"

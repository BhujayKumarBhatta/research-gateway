from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"
SECRET_NAMES = {
    "api_key",
    "apikey",
    "token",
    "authtoken",
    "institutional_token",
    "authorization",
    "x-els-apikey",
    "x-els-insttoken",
    "zotero-api-key",
    "mcp_remote_token",
    "github_token",
}
_BEARER_PATTERN = re.compile(r"(?i)(bearer\s+)[^\s,;]+")


def is_secret_name(name: str) -> bool:
    normalized = name.strip().lower().replace("_", "-")
    return normalized in {item.replace("_", "-") for item in SECRET_NAMES} or any(
        marker in normalized for marker in ("secret", "password", "credential")
    )


def redact(value: Any) -> Any:
    """Return a recursively redacted copy safe for logs and diagnostics."""
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if is_secret_name(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _BEARER_PATTERN.sub(r"\1[REDACTED]", value)
    return value


def redact_text(text: str, secret_values: Sequence[str] = ()) -> str:
    safe = _BEARER_PATTERN.sub(r"\1[REDACTED]", text)
    for secret in secret_values:
        if secret:
            safe = safe.replace(secret, REDACTED)
    return safe

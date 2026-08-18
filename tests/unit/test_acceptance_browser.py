from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_gateway.acceptance import _run_playwright_browser, _temporary_oauth_settings
from research_gateway.config import Settings


class _BrowserProcess:
    def __init__(self, result: dict[str, object], returncode: int) -> None:
        self.result = result
        self.returncode = returncode
        self.stdin_payload = b""

    async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
        self.stdin_payload = payload
        return json.dumps(self.result).encode(), b"browser-secret-must-stay-hidden"


@pytest.mark.asyncio
async def test_playwright_browser_receives_secrets_only_on_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _BrowserProcess(
        {
            "ok": True,
            "allow_clicks": 1,
            "events": [{"method": "POST", "path": "/oauth/authorize", "status": 302}],
        },
        0,
    )
    invocation: list[tuple[object, ...]] = []

    async def create_process(*args: object, **kwargs: object) -> _BrowserProcess:
        invocation.append((*args, kwargs))
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", create_process)
    script = tmp_path / "oauth-browser-ngrok.mjs"
    payload = {"authorization_url": "https://example.test/authorize", "password": "secret"}

    result = await _run_playwright_browser(script, payload)

    assert result["allow_clicks"] == 1
    assert "secret" not in " ".join(str(value) for value in invocation[0][:-1])
    assert json.loads(process.stdin_payload) == payload


@pytest.mark.asyncio
async def test_playwright_browser_failure_reports_only_known_safe_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _BrowserProcess({"ok": False, "phase": "approval-submit"}, 1)

    async def create_process(*args: object, **kwargs: object) -> _BrowserProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", create_process)

    with pytest.raises(RuntimeError, match="failed during approval-submit") as raised:
        await _run_playwright_browser(tmp_path / "browser.mjs", {"password": "secret"})

    assert "secret" not in str(raised.value)
    assert "browser-secret" not in str(raised.value)


def test_temporary_oauth_settings_are_isolated_from_the_live_domain(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "database": {"path": tmp_path / "live.db"},
            "tunnel": {"authtoken": "fixture-token", "domain": "live.example.test"},
        }
    )

    temporary, password = _temporary_oauth_settings(settings, tmp_path / "isolated")

    assert password
    assert temporary.tunnel.domain == ""
    assert temporary.database.path == tmp_path / "isolated" / "acceptance.db"
    assert temporary.mcp_oauth.store_path == tmp_path / "isolated" / "oauth.sqlite3"
    assert temporary.mcp_oauth.configured is True
    assert temporary.mcp_oauth.admin_password_hash.get_secret_value() != password
    assert settings.tunnel.domain == "live.example.test"

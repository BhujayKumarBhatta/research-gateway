from __future__ import annotations

import logging
from pathlib import Path

import pytest

from research_gateway.config import ConfigError, load_settings, resolve_config_path
from research_gateway.security import REDACTED, redact, redact_text


def test_global_config_path_uses_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("RESEARCH_GATEWAY_CONFIG", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert resolve_config_path() == tmp_path / ".research-gateway" / "config.toml"


def test_environment_config_path_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configured = tmp_path / "chosen.toml"
    monkeypatch.setenv("RESEARCH_GATEWAY_CONFIG", str(configured))

    assert resolve_config_path() == configured


def test_partial_config_merges_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[scopus]\napi_key = "fixture-scopus-secret"\n', encoding="utf-8")

    settings = load_settings(path)

    assert settings.service.host == "127.0.0.1"
    assert settings.service.port == 8765
    assert settings.scopus.configured is True
    assert settings.arxiv.enabled is True
    assert settings.database.path.is_absolute()


def test_invalid_toml_is_a_safe_configuration_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[scopus]\napi_key = broken\n", encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        load_settings(path)

    assert "broken" not in str(caught.value)
    assert str(path) in str(caught.value)


def test_redaction_removes_secret_values(caplog: pytest.LogCaptureFixture) -> None:
    secret = "fixture-super-secret"
    payload = {
        "api_key": secret,
        "nested": {"Authorization": f"Bearer {secret}"},
        "safe": "visible",
    }

    safe = redact(payload)
    logging.getLogger("research_gateway.test").warning("%s", safe)

    assert secret not in repr(safe)
    assert secret not in caplog.text
    assert safe["safe"] == "visible"


def test_redaction_handles_sequences_and_explicit_secret_values() -> None:
    secret = "fixture-explicit-secret"
    safe = redact([{"password": secret}, f"Bearer {secret}", 7])
    assert safe == [{"password": REDACTED}, f"Bearer {REDACTED}", 7]
    assert redact_text(f"failed with {secret}", [secret]) == f"failed with {REDACTED}"

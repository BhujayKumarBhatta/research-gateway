from __future__ import annotations

import tomllib
from pathlib import Path

from research_gateway.config import Settings, load_settings
from research_gateway.oauth.security import hash_password, verify_password
from research_gateway.oauth.setup import initialize_oauth, with_oauth_urls


def test_password_hash_is_salted_and_verifiable() -> None:
    password = "a sufficiently long password"
    first = hash_password(password)
    second = hash_password(password)

    assert first != second
    assert password not in first
    assert verify_password(password, first)
    assert not verify_password("wrong password value", first)


def test_oauth_init_preserves_provider_keys_and_stores_no_plain_password(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    password = "another sufficiently long password"
    config.write_text(
        f"""[database]
path = {str(tmp_path / "gateway.db")!r}
[runtime]
directory = {str(tmp_path / "runtime")!r}
[mcp_remote_auth]
mode = "static_bearer"
token = "legacy-token"
[scopus]
api_key = "provider-key-that-must-remain"
[github]
token = "github-key-that-must-remain"
""",
        encoding="utf-8",
    )

    result = initialize_oauth(config, password=password)
    raw_text = config.read_text(encoding="utf-8")
    raw = tomllib.loads(raw_text)
    settings = load_settings(config, require_file=True)

    assert result.generated_password is None
    assert settings.mcp_remote_auth.mode == "oauth"
    assert settings.mcp_oauth.configured
    assert verify_password(password, settings.mcp_oauth.admin_password_hash.get_secret_value())
    assert raw["scopus"]["api_key"] == "provider-key-that-must-remain"
    assert raw["github"]["token"] == "github-key-that-must-remain"
    assert raw["mcp_remote_auth"]["token"] == "legacy-token"
    assert password not in raw_text
    assert result.store_path.is_file()


def test_oauth_runtime_urls_use_active_ngrok_host() -> None:
    settings = Settings.model_validate(
        {
            "mcp_remote_auth": {"mode": "oauth"},
            "mcp_oauth": {
                "enabled": True,
                "admin_password_hash": "hash",
                "signing_secret": "sign",
                "sealing_secret": "seal",
            },
        }
    )

    resolved = with_oauth_urls(settings, "https://safe.ngrok.app/")

    assert resolved.mcp_oauth.issuer_url == "https://safe.ngrok.app"
    assert resolved.mcp_oauth.resource_url == "https://safe.ngrok.app/mcp"
    assert settings.mcp_oauth.issuer_url == ""

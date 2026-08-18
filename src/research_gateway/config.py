from __future__ import annotations

import os
import secrets
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator


class ConfigError(RuntimeError):
    """A safe configuration error that never embeds configuration contents."""


class ServiceSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(validate_default=True)

    path: Path = Path("~/.research-gateway/data/research_gateway.db")

    @field_validator("path")
    @classmethod
    def expand_path(cls, value: Path) -> Path:
        return value.expanduser().absolute()


class LoggingSettings(BaseModel):
    path: Path | None = None
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    max_bytes: int = Field(default=5_000_000, ge=100_000)
    backup_count: int = Field(default=5, ge=1, le=50)

    @field_validator("path")
    @classmethod
    def expand_optional_path(cls, value: Path | None) -> Path | None:
        return value.expanduser().absolute() if value else None


class BackupSettings(BaseModel):
    directory: Path | None = None
    enabled: bool = True
    on_service_start: bool = True
    retention_count: int = Field(default=20, ge=1, le=500)

    @field_validator("directory")
    @classmethod
    def expand_optional_directory(cls, value: Path | None) -> Path | None:
        return value.expanduser().absolute() if value else None


class RuntimeSettings(BaseModel):
    directory: Path | None = None

    @field_validator("directory")
    @classmethod
    def expand_optional_directory(cls, value: Path | None) -> Path | None:
        return value.expanduser().absolute() if value else None


class TunnelSettings(BaseModel):
    enabled: bool = True
    provider: Literal["ngrok"] = "ngrok"
    authtoken: SecretStr = SecretStr("")
    domain: str = ""
    expose_ui: bool = False
    start_on_serve: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.authtoken.get_secret_value())


class McpRemoteAuthSettings(BaseModel):
    mode: Literal["static_bearer", "oauth"] = "static_bearer"
    token: SecretStr = SecretStr("")
    allow_unauthenticated: bool = False

    @property
    def configured(self) -> bool:
        return self.mode == "static_bearer" and bool(self.token.get_secret_value())


class McpOAuthSettings(BaseModel):
    enabled: bool = False
    issuer_url: str = ""
    resource_url: str = ""
    scope: str = "research-gateway"
    admin_password_hash: SecretStr = SecretStr("")
    signing_secret: SecretStr = SecretStr("")
    sealing_secret: SecretStr = SecretStr("")
    store_path: Path | None = None
    access_token_minutes: int = Field(default=60, ge=1, le=1440)
    refresh_token_days: int = Field(default=30, ge=1, le=365)
    authorization_code_seconds: int = Field(default=300, ge=60, le=600)
    approval_request_seconds: int = Field(default=600, ge=60, le=1800)
    approval_completion_seconds: int = Field(default=90, ge=60, le=120)

    @field_validator("store_path")
    @classmethod
    def expand_optional_path(cls, value: Path | None) -> Path | None:
        return value.expanduser().absolute() if value else None

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.admin_password_hash.get_secret_value()
            and self.signing_secret.get_secret_value()
            and self.sealing_secret.get_secret_value()
        )


class ScopusSettings(BaseModel):
    enabled: bool = True
    api_key: SecretStr = SecretStr("")
    institutional_token: SecretStr = SecretStr("")
    base_url: str = "https://api.elsevier.com"

    @property
    def configured(self) -> bool:
        return bool(self.api_key.get_secret_value())


class WosSettings(BaseModel):
    enabled: bool = False
    mode: Literal["starter", "expanded"] = "starter"
    approval_status: Literal["pending", "active", "denied"] = "pending"
    api_key: SecretStr = SecretStr("")
    base_url: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key.get_secret_value())

    @property
    def approved(self) -> bool:
        return self.approval_status == "active"


class IeeeSettings(BaseModel):
    enabled: bool = False
    approval_status: Literal["pending", "active", "denied"] = "pending"
    api_key: SecretStr = SecretStr("")
    base_url: str = "https://ieeexploreapi.ieee.org"
    query_field: Literal["querytext", "meta_data"] = "querytext"

    @property
    def configured(self) -> bool:
        return bool(self.api_key.get_secret_value())

    @property
    def approved(self) -> bool:
        return self.approval_status == "active"


class AclSettings(BaseModel):
    model_config = ConfigDict(validate_default=True)

    enabled: bool = True
    index_path: Path = Path("~/.research-gateway/indexes/acl/index.json")

    @field_validator("index_path")
    @classmethod
    def expand_path(cls, value: Path) -> Path:
        return value.expanduser().absolute()


class ArxivSettings(BaseModel):
    enabled: bool = True
    base_url: str = "https://export.arxiv.org/api/query"
    polite_delay_seconds: float = Field(default=3.0, ge=0)


class AcmSettings(BaseModel):
    enabled: bool = False


class ZoteroSettings(BaseModel):
    enabled: bool = False
    api_key: SecretStr = SecretStr("")
    library_type: Literal["user", "group"] = "user"
    library_id: str = ""
    collection_key: str = ""
    collection_name: str = ""
    base_url: str = "https://api.zotero.org"

    @property
    def configured(self) -> bool:
        return bool(self.api_key.get_secret_value() and self.library_id)


class GithubSettings(BaseModel):
    enabled: bool = False
    token: SecretStr = SecretStr("")
    api_url: str = "https://api.github.com"
    default_owner: str = ""
    default_repository: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.token.get_secret_value())


class RetentionSettings(BaseModel):
    licensed_raw_metadata_default: Literal["full", "minimal", "none"] = "minimal"
    open_raw_metadata_default: Literal["full", "minimal", "none"] = "full"


class Settings(BaseModel):
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    backup: BackupSettings = Field(default_factory=BackupSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    tunnel: TunnelSettings = Field(default_factory=TunnelSettings)
    mcp_remote_auth: McpRemoteAuthSettings = Field(default_factory=McpRemoteAuthSettings)
    mcp_oauth: McpOAuthSettings = Field(default_factory=McpOAuthSettings)
    scopus: ScopusSettings = Field(default_factory=ScopusSettings)
    wos: WosSettings = Field(default_factory=WosSettings)
    ieee_xplore: IeeeSettings = Field(default_factory=IeeeSettings)
    acl_anthology: AclSettings = Field(default_factory=AclSettings)
    arxiv: ArxivSettings = Field(default_factory=ArxivSettings)
    acm_dl: AcmSettings = Field(default_factory=AcmSettings)
    zotero: ZoteroSettings = Field(default_factory=ZoteroSettings)
    github: GithubSettings = Field(default_factory=GithubSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)

    @property
    def remote_auth_configured(self) -> bool:
        if self.mcp_remote_auth.allow_unauthenticated:
            return True
        if self.mcp_remote_auth.mode == "oauth":
            return self.mcp_oauth.configured
        return bool(self.mcp_remote_auth.token.get_secret_value())

    def secret_values(self) -> list[str]:
        return [
            self.tunnel.authtoken.get_secret_value(),
            self.mcp_remote_auth.token.get_secret_value(),
            self.mcp_oauth.admin_password_hash.get_secret_value(),
            self.mcp_oauth.signing_secret.get_secret_value(),
            self.mcp_oauth.sealing_secret.get_secret_value(),
            self.scopus.api_key.get_secret_value(),
            self.scopus.institutional_token.get_secret_value(),
            self.wos.api_key.get_secret_value(),
            self.ieee_xplore.api_key.get_secret_value(),
            self.zotero.api_key.get_secret_value(),
            self.github.token.get_secret_value(),
        ]


def resolve_config_path() -> Path:
    override = os.environ.get("RESEARCH_GATEWAY_CONFIG")
    if override:
        return Path(override).expanduser().absolute()
    return Path.home() / ".research-gateway" / "config.toml"


def load_settings(path: Path | None = None, *, require_file: bool = False) -> Settings:
    selected = (path or resolve_config_path()).expanduser().absolute()
    payload: dict[str, Any] = {}
    if selected.exists():
        try:
            with selected.open("rb") as stream:
                payload = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"Configuration file is not readable valid TOML: {selected}") from exc
    elif require_file:
        raise ConfigError(f"Configuration file does not exist: {selected}")
    try:
        return Settings.model_validate(payload)
    except ValidationError as exc:
        raise ConfigError(f"Configuration values are invalid in: {selected}") from exc


def generate_remote_token() -> str:
    return secrets.token_urlsafe(48)

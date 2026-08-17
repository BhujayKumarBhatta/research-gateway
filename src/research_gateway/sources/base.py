from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from research_gateway.domain.models import ProviderRetentionPolicy, SourcePage


class ProviderError(RuntimeError):
    error_type = "provider_error"
    retryable = False

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.safe_message = message
        self.status_code = status_code


class ProviderConfigurationError(ProviderError):
    error_type = "configuration_error"


class ProviderUnavailableError(ProviderError):
    error_type = "provider_unavailable"


class ProviderAuthenticationError(ProviderError):
    error_type = "authentication_error"


class ProviderEntitlementError(ProviderError):
    error_type = "entitlement_error"


class ProviderRateLimitError(ProviderError):
    error_type = "rate_limit_error"
    retryable = True


class ProviderTimeoutError(ProviderError):
    error_type = "provider_timeout"
    retryable = True


class ProviderUpstreamError(ProviderError):
    error_type = "provider_error"
    retryable = True


class ProviderPayloadError(ProviderError):
    error_type = "provider_payload_error"


class ProviderStatus(BaseModel):
    name: str
    enabled: bool
    configured: bool
    available: bool
    credential_requirement: str | None = None
    read_capabilities: list[str] = Field(default_factory=list)
    write_capabilities: list[str] = Field(default_factory=list)
    retention_policy: ProviderRetentionPolicy
    paging_notes: str = ""
    unavailable_reason: str | None = None
    last_connectivity: dict[str, Any] | None = None


class SourceAdapter(ABC):
    name: str
    retention_policy: ProviderRetentionPolicy

    @property
    @abstractmethod
    def status(self) -> ProviderStatus:
        raise NotImplementedError

    @abstractmethod
    async def count(self, query: str, *, filters: dict[str, Any] | None = None) -> int:
        raise NotImplementedError

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
        filters: dict[str, Any] | None = None,
        sort: dict[str, Any] | None = None,
    ) -> SourcePage:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def clean_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def clean_year(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    for index in range(max(0, len(text) - 3)):
        candidate = text[index : index + 4]
        if candidate.isdigit() and 1000 <= int(candidate) <= 2999:
            return int(candidate)
    return None


def safe_http_error(provider: str, status_code: int) -> ProviderError:
    if status_code == 401:
        return ProviderAuthenticationError(
            f"{provider} rejected the configured credential.", status_code=status_code
        )
    if status_code == 403:
        return ProviderAuthenticationError(
            f"{provider} authentication or entitlement was rejected.", status_code=status_code
        )
    if status_code == 429:
        return ProviderRateLimitError(
            f"{provider} rate limit was reached.", status_code=status_code
        )
    if status_code >= 500:
        return ProviderUpstreamError(
            f"{provider} upstream service is unavailable.", status_code=status_code
        )
    return ProviderUpstreamError(
        f"{provider} request failed with HTTP {status_code}.", status_code=status_code
    )

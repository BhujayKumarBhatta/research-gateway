from __future__ import annotations

from typing import Any

from research_gateway.config import AcmSettings
from research_gateway.domain.models import ProviderRetentionPolicy, SourcePage
from research_gateway.sources.base import ProviderStatus, ProviderUnavailableError, SourceAdapter


class AcmDlAdapter(SourceAdapter):
    name = "acm_dl"
    retention_policy = ProviderRetentionPolicy(
        raw_metadata="none",
        abstract_storage="restricted",
        max_page_size=0,
        terms_reference="https://www.acm.org/diversity-inclusion/equity-through-oa",
    )

    def __init__(self, settings: AcmSettings) -> None:
        self.settings = settings

    @property
    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            enabled=False,
            configured=False,
            available=False,
            credential_requirement=None,
            read_capabilities=[],
            retention_policy=self.retention_policy,
            unavailable_reason="official_programmatic_search_not_verified",
        )

    async def count(self, query: str, *, filters: dict[str, Any] | None = None) -> int:
        raise ProviderUnavailableError("ACM Digital Library official search API is not verified.")

    async def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
        filters: dict[str, Any] | None = None,
        sort: dict[str, Any] | None = None,
    ) -> SourcePage:
        raise ProviderUnavailableError("ACM Digital Library official search API is not verified.")

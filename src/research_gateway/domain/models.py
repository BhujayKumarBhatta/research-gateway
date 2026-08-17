from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class SearchMode(StrEnum):
    EXPLORE = "explore"
    SAVE = "save"


class ScreeningStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    CANDIDATE = "candidate"
    INCLUDED = "included"
    EXCLUDED = "excluded"
    FINAL = "final"


class SourceRecord(BaseModel):
    provider: str
    provider_record_id: str
    title: str | None = None
    authors: list[dict[str, Any]] = Field(default_factory=list)
    abstract: str | None = None
    year: int | None = None
    publication_date: str | None = None
    publication: str | None = None
    doi: str | None = None
    url: str | None = None
    document_type: str | None = None
    keywords: list[str] = Field(default_factory=list)
    citation_count: int | None = None
    open_access: dict[str, Any] | None = None
    identifiers: dict[str, str] = Field(default_factory=dict)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class SourcePage(BaseModel):
    provider: str
    provider_query: str
    total_results: int
    offset: int = 0
    returned_count: int
    next_offset: int | None = None
    records: list[SourceRecord] = Field(default_factory=list)
    pagination: dict[str, Any] = Field(default_factory=dict)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class SearchRun(BaseModel):
    search_run_id: str
    search_code: str
    study_id: str
    topic_id: str | None
    provider: str
    mode: SearchMode
    label: str
    search_intent: str
    provider_query: str
    filters: dict[str, Any]
    sort: dict[str, Any]
    requested_limit: int
    executed_at_utc: datetime
    status: str


class IngestResult(BaseModel):
    evidence_id: str
    created: bool
    matched_by: str | None = None


class Page(BaseModel):
    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int


class ProviderRetentionPolicy(BaseModel):
    raw_metadata: Literal["full", "minimal", "none"]
    abstract_storage: Literal["allowed", "restricted"]
    full_text_storage: Literal["never"] = "never"
    max_page_size: int
    terms_reference: str

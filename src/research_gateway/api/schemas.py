from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from research_gateway.domain.models import ScreeningStatus


class StudyCreate(BaseModel):
    study_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    description: str = ""
    system_test: bool = False


class TopicCreate(BaseModel):
    topic_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    description: str = ""


class SearchRequest(BaseModel):
    study_id: str
    topic_id: str | None = None
    provider: str
    search_intent: str
    provider_query: str
    label: str = ""
    requested_limit: int = Field(default=25, ge=1, le=10_000)
    filters: dict[str, Any] = Field(default_factory=dict)
    sort: dict[str, Any] = Field(default_factory=dict)


class ScreeningUpdate(BaseModel):
    status: ScreeningStatus
    reason: str | None = None
    note: str | None = None
    actor: str = "ui"


class NoteCreate(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    actor: str = "ui"


class ExportRequest(BaseModel):
    path: str
    format: Literal["json", "csv", "xlsx", "markdown"]
    study_id: str | None = None
    topic_id: str | None = None
    final_only: bool = False


class ZoteroSyncRequest(BaseModel):
    study_id: str | None = None
    dry_run: bool = True


class GithubPublishRequest(BaseModel):
    repository: str
    branch: str
    files: dict[str, str]
    commit_message: str
    pull_request_title: str
    pull_request_body: str
    dry_run: bool = True

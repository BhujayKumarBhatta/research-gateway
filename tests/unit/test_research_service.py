from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from research_gateway.db.database import EvidenceDatabase
from research_gateway.domain.models import ProviderRetentionPolicy, SourcePage, SourceRecord
from research_gateway.services.research import ResearchService
from research_gateway.sources.base import ProviderStatus, SourceAdapter
from research_gateway.sources.registry import SourceRegistry


class FixtureAdapter(SourceAdapter):
    name = "fixture"
    retention_policy = ProviderRetentionPolicy(
        raw_metadata="minimal",
        abstract_storage="restricted",
        max_page_size=1,
        terms_reference="https://example.test/terms",
    )

    @property
    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            enabled=True,
            configured=True,
            available=True,
            read_capabilities=["count", "search"],
            retention_policy=self.retention_policy,
        )

    async def count(self, query: str, *, filters: dict[str, Any] | None = None) -> int:
        return 2

    async def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
        filters: dict[str, Any] | None = None,
        sort: dict[str, Any] | None = None,
    ) -> SourcePage:
        records = [
            SourceRecord(
                provider=self.name,
                provider_record_id=f"r{offset + 1}",
                title=f"A sufficiently long research title {offset + 1}",
                authors=[{"name": "Ada Lovelace"}],
                abstract="licensed abstract",
                year=2025,
                doi="10.1000/shared" if offset == 1 else "10.1000/one",
                raw_metadata={"article_number": str(offset + 1), "secret_blob": "discard"},
            )
        ]
        return SourcePage(
            provider=self.name,
            provider_query=query,
            total_results=2,
            offset=offset,
            returned_count=1,
            next_offset=offset + 1 if offset == 0 else None,
            records=records,
            pagination={"offset": offset},
        )


@pytest.fixture
async def service(tmp_path: Path) -> tuple[ResearchService, EvidenceDatabase]:
    database = EvidenceDatabase(tmp_path / "evidence.db")
    await database.migrate()
    await database.create_study("s1", "Study", "Purpose")
    await database.create_topic("s1", "t1", "Topic", "Question")
    return ResearchService(database, SourceRegistry([FixtureAdapter()])), database


@pytest.mark.asyncio
async def test_explore_records_count_without_discoveries(
    service: tuple[ResearchService, EvidenceDatabase],
) -> None:
    research, database = service
    result = await research.explore(
        study_id="s1",
        topic_id="t1",
        provider="fixture",
        search_intent="Estimate scope",
        provider_query='TITLE("language model")',
    )
    assert result["provider_reported_total"] == 2
    assert await database.count_rows("search_runs") == 1
    assert await database.count_rows("search_hits") == 0
    assert await database.count_rows("evidence") == 0


@pytest.mark.asyncio
async def test_save_pages_and_applies_retention(
    service: tuple[ResearchService, EvidenceDatabase],
) -> None:
    research, database = service
    result = await research.save(
        study_id="s1",
        topic_id="t1",
        provider="fixture",
        search_intent="Build corpus",
        provider_query='TITLE("language model")',
        requested_limit=2,
    )
    assert result["retrieved_count"] == 2
    assert result["new_evidence_count"] == 2
    assert await database.count_rows("search_hits") == 2
    page = await database.list_evidence(study_id="s1")
    assert page.total == 2
    details = await database.get_evidence(page.items[0]["evidence_id"])
    assert details and details["abstract"] is None
    run = await database.get_search_run(result["search_run_id"])
    assert run and run["provider_query"] == 'TITLE("language model")'
    assert len(await database.list_audit_events()) == 1


@pytest.mark.asyncio
async def test_same_doi_becomes_one_evidence_with_two_discoveries(
    service: tuple[ResearchService, EvidenceDatabase],
) -> None:
    research, database = service
    first = await research.save(
        study_id="s1",
        topic_id="t1",
        provider="fixture",
        search_intent="one",
        provider_query="query",
        requested_limit=2,
    )
    second = await research.save(
        study_id="s1",
        topic_id="t1",
        provider="fixture",
        search_intent="two",
        provider_query="query",
        requested_limit=2,
    )
    assert first["new_evidence_count"] == 2
    assert second["new_evidence_count"] == 0
    assert second["existing_evidence_count"] == 2
    assert await database.count_rows("evidence") == 2
    assert await database.count_rows("search_hits") == 4

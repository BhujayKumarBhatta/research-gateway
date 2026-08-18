from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from research_gateway.db.database import EvidenceDatabase
from research_gateway.domain.models import ScreeningStatus, SourceRecord


@pytest.fixture
async def database(tmp_path: Path) -> EvidenceDatabase:
    db = EvidenceDatabase(tmp_path / "evidence.db")
    await db.migrate()
    return db


@pytest.mark.asyncio
async def test_migrations_create_versioned_schema(database: EvidenceDatabase) -> None:
    assert await database.user_version() >= 2
    tables = await database.table_names()
    assert {
        "studies",
        "topics",
        "search_runs",
        "search_hits",
        "evidence",
        "evidence_identifiers",
        "screening_events",
        "audit_events",
    }.issubset(tables)


@pytest.mark.asyncio
async def test_v1_database_migrates_classification_columns_without_data_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1.db"
    async with aiosqlite.connect(path) as connection:
        await connection.execute(
            "CREATE TABLE evidence ("
            "evidence_id TEXT PRIMARY KEY, evidence_code TEXT NOT NULL, normalized_doi TEXT, "
            "bibliographic_fingerprint TEXT, screening_status TEXT NOT NULL DEFAULT 'unreviewed', "
            "final_corpus INTEGER NOT NULL DEFAULT 0)"
        )
        await connection.execute(
            "INSERT INTO evidence(evidence_id,evidence_code) VALUES('old-id', 'E000001')"
        )
        await connection.execute("PRAGMA user_version = 1")
        await connection.commit()

    migrated = EvidenceDatabase(path)
    await migrated.migrate()

    async with aiosqlite.connect(path) as connection:
        cursor = await connection.execute("PRAGMA table_info(evidence)")
        columns = {row[1] for row in await cursor.fetchall()}
        row = await (
            await connection.execute(
                "SELECT evidence_id,publication_type,review_status FROM evidence"
            )
        ).fetchone()
    assert {"publication_type", "review_status"}.issubset(columns)
    assert row == ("old-id", None, "unknown")


@pytest.mark.asyncio
async def test_study_topic_and_search_codes_are_scoped(database: EvidenceDatabase) -> None:
    await database.create_study("study-a", "Study A", "A reproducible review")
    await database.create_topic("study-a", "topic-a", "Topic A", "Failure types")

    first = await database.create_search_run(
        study_id="study-a",
        topic_id="topic-a",
        provider="scopus",
        mode="explore",
        label="broad",
        search_intent="Find broad work",
        provider_query='TITLE-ABS-KEY("fine tuning")',
        filters={},
        sort={},
        requested_limit=3,
    )
    second = await database.create_search_run(
        study_id="study-a",
        topic_id="topic-a",
        provider="arxiv",
        mode="save",
        label="open",
        search_intent="Find open work",
        provider_query='all:"fine tuning"',
        filters={},
        sort={},
        requested_limit=3,
    )

    assert first.search_code == "Q0001"
    assert second.search_code == "Q0002"
    assert first.provider_query == 'TITLE-ABS-KEY("fine tuning")'


@pytest.mark.asyncio
async def test_explore_completion_creates_no_hits_or_evidence(database: EvidenceDatabase) -> None:
    await database.create_study("study-a", "Study A", "")
    run = await database.create_search_run(
        study_id="study-a",
        topic_id=None,
        provider="scopus",
        mode="explore",
        label="preview",
        search_intent="Tune a query",
        provider_query="TITLE(test)",
        filters={},
        sort={},
        requested_limit=2,
    )
    await database.complete_search_run(
        run.search_run_id,
        provider_reported_total=10,
        retrieved_count=2,
        complete=False,
        pagination={"offset": 0, "next_offset": 2},
    )

    assert await database.count_rows("search_runs") == 1
    assert await database.count_rows("search_hits") == 0
    assert await database.count_rows("evidence") == 0


@pytest.mark.asyncio
async def test_doi_dedup_keeps_every_discovery(database: EvidenceDatabase) -> None:
    await database.create_study("study-a", "Study A", "")
    scopus_run = await database.create_search_run(
        study_id="study-a",
        topic_id=None,
        provider="scopus",
        mode="save",
        label="licensed",
        search_intent="Find papers",
        provider_query="TITLE(test)",
        filters={},
        sort={},
        requested_limit=1,
    )
    arxiv_run = await database.create_search_run(
        study_id="study-a",
        topic_id=None,
        provider="arxiv",
        mode="save",
        label="open",
        search_intent="Find preprints",
        provider_query="all:test",
        filters={},
        sort={},
        requested_limit=1,
    )
    scopus = SourceRecord(
        provider="scopus",
        provider_record_id="2-s2.0-123",
        title="A Careful Study",
        authors=[{"name": "Ada Lovelace"}],
        year=2025,
        doi="https://doi.org/10.1000/ABC",
        identifiers={"scopus_eid": "2-s2.0-123"},
        raw_metadata={"eid": "2-s2.0-123"},
    )
    arxiv = SourceRecord(
        provider="arxiv",
        provider_record_id="2501.01234",
        title="A Careful Study",
        authors=[{"name": "Ada Lovelace"}],
        year=2025,
        doi="doi:10.1000/abc",
        identifiers={"arxiv_id": "2501.01234"},
        raw_metadata={"id": "2501.01234"},
    )

    first = await database.ingest_search_hit(scopus_run.search_run_id, 1, scopus)
    second = await database.ingest_search_hit(arxiv_run.search_run_id, 1, arxiv)

    assert first.evidence_id == second.evidence_id
    assert await database.count_rows("evidence") == 1
    assert await database.count_rows("search_hits") == 2
    discoveries = await database.list_discoveries(first.evidence_id)
    assert {item["provider"] for item in discoveries} == {"scopus", "arxiv"}


@pytest.mark.asyncio
async def test_uncertain_title_match_is_not_merged(database: EvidenceDatabase) -> None:
    await database.create_study("study-a", "Study A", "")
    run = await database.create_search_run(
        study_id="study-a",
        topic_id=None,
        provider="acl_anthology",
        mode="save",
        label="acl",
        search_intent="Find papers",
        provider_query="title:fine-tuning",
        filters={},
        sort={},
        requested_limit=2,
    )
    original = SourceRecord(
        provider="acl_anthology",
        provider_record_id="2025.acl-1.1",
        title="Fine Tuning Systems",
        authors=[{"name": "First Author"}],
        year=2025,
        identifiers={"acl_id": "2025.acl-1.1"},
    )
    uncertain = SourceRecord(
        provider="acl_anthology",
        provider_record_id="2025.acl-1.2",
        title="Fine-Tuning Systems: Extended Version",
        authors=[{"name": "First Author"}],
        year=2025,
        identifiers={"acl_id": "2025.acl-1.2"},
    )

    first = await database.ingest_search_hit(run.search_run_id, 1, original)
    second = await database.ingest_search_hit(run.search_run_id, 2, uncertain)

    assert first.evidence_id != second.evidence_id
    assert await database.count_rows("evidence") == 2
    duplicates = await database.list_possible_duplicates()
    assert len(duplicates) == 1
    assert duplicates[0]["status"] == "open"


@pytest.mark.asyncio
async def test_conflicting_dois_prevent_fingerprint_merge(database: EvidenceDatabase) -> None:
    await database.create_study("study-conflict", "Conflicting DOI study", "")
    run = await database.create_search_run(
        study_id="study-conflict",
        topic_id=None,
        provider="fixture",
        mode="save",
        label="",
        search_intent="Check identifiers",
        provider_query="query",
        filters={},
        sort={},
        requested_limit=2,
    )
    common = {
        "provider": "fixture",
        "title": "The Same Long Bibliographic Title",
        "authors": [{"name": "Same Author"}],
        "year": 2025,
    }
    first = await database.ingest_search_hit(
        run.search_run_id,
        1,
        SourceRecord(**common, provider_record_id="one", doi="10.1000/one"),
    )
    second = await database.ingest_search_hit(
        run.search_run_id,
        2,
        SourceRecord(**common, provider_record_id="two", doi="10.1000/two"),
    )
    assert first.evidence_id != second.evidence_id
    assert await database.count_rows("evidence") == 2
    assert len(await database.list_possible_duplicates()) == 1


@pytest.mark.asyncio
async def test_screening_history_and_final_corpus(database: EvidenceDatabase) -> None:
    await database.create_study("study-a", "Study A", "")
    run = await database.create_search_run(
        study_id="study-a",
        topic_id=None,
        provider="arxiv",
        mode="save",
        label="open",
        search_intent="Find papers",
        provider_query="all:test",
        filters={},
        sort={},
        requested_limit=1,
    )
    ingested = await database.ingest_search_hit(
        run.search_run_id,
        1,
        SourceRecord(
            provider="arxiv",
            provider_record_id="2501.00001",
            title="A Candidate Paper",
            authors=[{"name": "Researcher One"}],
            year=2025,
            identifiers={"arxiv_id": "2501.00001"},
        ),
    )

    await database.set_screening(
        ingested.evidence_id,
        ScreeningStatus.INCLUDED,
        reason=None,
        note="Meets criteria",
        actor="test",
    )
    await database.set_screening(
        ingested.evidence_id,
        ScreeningStatus.FINAL,
        reason=None,
        note="Final corpus",
        actor="test",
    )

    history = await database.screening_history(ingested.evidence_id)
    assert [item["new_status"] for item in history] == ["included", "final"]
    final = await database.list_evidence(study_id="study-a", final=True)
    assert [item["evidence_id"] for item in final.items] == [ingested.evidence_id]

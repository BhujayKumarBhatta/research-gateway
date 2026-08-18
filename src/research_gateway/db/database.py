from __future__ import annotations

import json
import re
import unicodedata
import uuid
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import aiosqlite

from research_gateway.domain.models import (
    IngestResult,
    Page,
    ScreeningStatus,
    SearchMode,
    SearchRun,
    SourceRecord,
)

SCHEMA_VERSION = 3
_SAFE_TABLES = {
    "studies",
    "topics",
    "search_runs",
    "search_hits",
    "evidence",
    "evidence_identifiers",
    "screening_events",
    "notes",
    "audit_events",
    "possible_duplicates",
    "zotero_links",
    "citation_references",
    "github_operations",
}


def _utc_text() -> str:
    return datetime.now(UTC).isoformat()


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    normalized = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", normalized)
    return normalized.rstrip(" .") or None


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", decomposed).split())


def bibliographic_fingerprint(record: SourceRecord) -> str | None:
    title = normalize_text(record.title)
    if len(title) < 12 or not record.year or not record.authors:
        return None
    first_author = normalize_text(str(record.authors[0].get("name") or ""))
    if not first_author:
        return None
    return f"{title}|{record.year}|{first_author}"


class EvidenceDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().absolute()

    async def _connect(self) -> aiosqlite.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    async def migrate(self) -> None:
        connection = await self._connect()
        try:
            await connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS studies (
                    study_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    system_test INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS topics (
                    topic_id TEXT PRIMARY KEY,
                    study_id TEXT NOT NULL REFERENCES studies(study_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(study_id, topic_id)
                );
                CREATE TABLE IF NOT EXISTS search_runs (
                    search_run_id TEXT PRIMARY KEY,
                    search_code TEXT NOT NULL,
                    study_id TEXT NOT NULL REFERENCES studies(study_id) ON DELETE CASCADE,
                    topic_id TEXT REFERENCES topics(topic_id) ON DELETE SET NULL,
                    provider TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('explore', 'save')),
                    label TEXT NOT NULL DEFAULT '',
                    search_intent TEXT NOT NULL,
                    provider_query TEXT NOT NULL,
                    filters_json TEXT NOT NULL DEFAULT '{}',
                    sort_json TEXT NOT NULL DEFAULT '{}',
                    executed_at_utc TEXT NOT NULL,
                    provider_reported_total INTEGER,
                    retrieved_count INTEGER NOT NULL DEFAULT 0,
                    requested_limit INTEGER NOT NULL,
                    is_complete INTEGER NOT NULL DEFAULT 0,
                    pagination_json TEXT NOT NULL DEFAULT '{}',
                    provider_metadata_json TEXT NOT NULL DEFAULT '{}',
                    new_evidence_count INTEGER NOT NULL DEFAULT 0,
                    existing_evidence_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'running',
                    duration_ms INTEGER,
                    error_type TEXT,
                    error_summary TEXT,
                    UNIQUE(study_id, search_code)
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    evidence_code TEXT NOT NULL UNIQUE,
                    title TEXT,
                    normalized_title TEXT NOT NULL DEFAULT '',
                    authors_json TEXT NOT NULL DEFAULT '[]',
                    author_names TEXT NOT NULL DEFAULT '',
                    abstract TEXT,
                    year INTEGER,
                    publication_date TEXT,
                    publication TEXT,
                    doi TEXT,
                    normalized_doi TEXT,
                    url TEXT,
                    document_type TEXT,
                    publication_type TEXT,
                    review_status TEXT NOT NULL DEFAULT 'unknown',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    citation_count INTEGER,
                    citation_source TEXT,
                    citation_timestamp TEXT,
                    open_access_json TEXT,
                    bibliographic_fingerprint TEXT,
                    screening_status TEXT NOT NULL DEFAULT 'unreviewed',
                    exclusion_reason TEXT,
                    final_corpus INTEGER NOT NULL DEFAULT 0,
                    notes_summary TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_doi
                    ON evidence(normalized_doi) WHERE normalized_doi IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_evidence_fingerprint
                    ON evidence(bibliographic_fingerprint);
                CREATE INDEX IF NOT EXISTS idx_evidence_screening
                    ON evidence(screening_status, final_corpus);
                CREATE TABLE IF NOT EXISTS evidence_identifiers (
                    identifier_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
                    identifier_type TEXT NOT NULL,
                    identifier_value TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    source_provider TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(identifier_type, normalized_value)
                );
                CREATE INDEX IF NOT EXISTS idx_identifier_evidence
                    ON evidence_identifiers(evidence_id);
                CREATE TABLE IF NOT EXISTS search_hits (
                    search_hit_id TEXT PRIMARY KEY,
                    search_run_id TEXT NOT NULL
                        REFERENCES search_runs(search_run_id) ON DELETE CASCADE,
                    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    provider_record_id TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    normalized_json TEXT NOT NULL,
                    raw_metadata_json TEXT NOT NULL DEFAULT '{}',
                    discovered_at TEXT NOT NULL,
                    UNIQUE(search_run_id, rank),
                    UNIQUE(search_run_id, provider_record_id)
                );
                CREATE INDEX IF NOT EXISTS idx_hits_evidence ON search_hits(evidence_id);
                CREATE INDEX IF NOT EXISTS idx_runs_filters
                    ON search_runs(study_id, topic_id, provider, mode, executed_at_utc, status);
                CREATE TABLE IF NOT EXISTS screening_events (
                    screening_event_id TEXT PRIMARY KEY,
                    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
                    timestamp_utc TEXT NOT NULL,
                    old_status TEXT NOT NULL,
                    new_status TEXT NOT NULL,
                    reason TEXT,
                    note TEXT,
                    actor TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notes (
                    note_id TEXT PRIMARY KEY,
                    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
                    text TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    audit_event_id TEXT PRIMARY KEY,
                    timestamp_utc TEXT NOT NULL,
                    study_id TEXT,
                    topic_id TEXT,
                    operation TEXT NOT NULL,
                    source TEXT,
                    entity_type TEXT,
                    entity_id TEXT,
                    status TEXT NOT NULL,
                    duration_ms INTEGER,
                    safe_summary TEXT,
                    error_type TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_audit_recent ON audit_events(timestamp_utc DESC);
                CREATE TABLE IF NOT EXISTS possible_duplicates (
                    possible_duplicate_id TEXT PRIMARY KEY,
                    evidence_id_a TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
                    evidence_id_b TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    UNIQUE(evidence_id_a, evidence_id_b)
                );
                CREATE TABLE IF NOT EXISTS zotero_links (
                    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
                    library_type TEXT NOT NULL,
                    library_id TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY(evidence_id, library_type, library_id)
                );
                CREATE INDEX IF NOT EXISTS idx_zotero_links_item
                    ON zotero_links(library_type, library_id, item_key);
                CREATE TABLE IF NOT EXISTS citation_references (
                    citation_reference_id TEXT PRIMARY KEY,
                    manuscript TEXT NOT NULL,
                    citation_location TEXT,
                    library_type TEXT NOT NULL,
                    library_id TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    evidence_id TEXT REFERENCES evidence(evidence_id) ON DELETE SET NULL,
                    identifier TEXT,
                    rationale TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_citation_references_manuscript
                    ON citation_references(manuscript, created_at);
                CREATE TABLE IF NOT EXISTS github_operations (
                    operation_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    dry_run INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    safe_summary TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(
                    evidence_id UNINDEXED, title, abstract, authors, publication, keywords
                );
                """
            )
            columns = {
                str(row[1])
                for row in await (
                    await connection.execute("PRAGMA table_info(evidence)")
                ).fetchall()
            }
            if "publication_type" not in columns:
                await connection.execute("ALTER TABLE evidence ADD COLUMN publication_type TEXT")
            if "review_status" not in columns:
                await connection.execute(
                    "ALTER TABLE evidence ADD COLUMN review_status TEXT NOT NULL DEFAULT 'unknown'"
                )
            await connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await connection.commit()
        finally:
            await connection.close()

    async def user_version(self) -> int:
        connection = await self._connect()
        try:
            row = await (await connection.execute("PRAGMA user_version")).fetchone()
            return int(row[0])
        finally:
            await connection.close()

    async def table_names(self) -> set[str]:
        connection = await self._connect()
        try:
            rows = await (
                await connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
            return {str(row[0]) for row in rows}
        finally:
            await connection.close()

    async def count_rows(self, table: str) -> int:
        if table not in _SAFE_TABLES:
            raise ValueError("Unknown table")
        connection = await self._connect()
        try:
            row = await (await connection.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            return int(row[0])
        finally:
            await connection.close()

    async def create_study(
        self,
        study_id: str,
        name: str,
        description: str,
        *,
        system_test: bool = False,
    ) -> dict[str, Any]:
        now = _utc_text()
        connection = await self._connect()
        try:
            await connection.execute(
                "INSERT INTO studies(study_id,name,description,system_test,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (study_id, name, description, int(system_test), now, now),
            )
            await connection.commit()
            return {"study_id": study_id, "name": name, "description": description}
        finally:
            await connection.close()

    async def create_topic(
        self, study_id: str, topic_id: str, name: str, description: str
    ) -> dict[str, Any]:
        now = _utc_text()
        connection = await self._connect()
        try:
            await connection.execute(
                "INSERT INTO topics(topic_id,study_id,name,description,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (topic_id, study_id, name, description, now, now),
            )
            await connection.commit()
            return {"topic_id": topic_id, "study_id": study_id, "name": name}
        finally:
            await connection.close()

    async def get_study(self, study_id: str) -> dict[str, Any] | None:
        return await self._fetch_one("SELECT * FROM studies WHERE study_id=?", (study_id,))

    async def list_studies(self) -> list[dict[str, Any]]:
        return await self._fetch_all("SELECT * FROM studies ORDER BY created_at")

    async def list_topics(self, study_id: str) -> list[dict[str, Any]]:
        return await self._fetch_all(
            "SELECT * FROM topics WHERE study_id=? ORDER BY created_at", (study_id,)
        )

    async def get_topic(self, topic_id: str) -> dict[str, Any] | None:
        return await self._fetch_one("SELECT * FROM topics WHERE topic_id=?", (topic_id,))

    async def update_topic(
        self,
        topic_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        current = await self.get_topic(topic_id)
        if not current:
            raise KeyError(topic_id)
        connection = await self._connect()
        try:
            await connection.execute(
                "UPDATE topics SET name=?,description=?,status=?,updated_at=? WHERE topic_id=?",
                (
                    name if name is not None else current["name"],
                    description if description is not None else current["description"],
                    status if status is not None else current["status"],
                    _utc_text(),
                    topic_id,
                ),
            )
            await connection.commit()
        finally:
            await connection.close()
        result = await self.get_topic(topic_id)
        assert result is not None
        return result

    async def get_search_run(self, search_run_id: str) -> dict[str, Any] | None:
        row = await self._fetch_one(
            "SELECT * FROM search_runs WHERE search_run_id=?", (search_run_id,)
        )
        if row:
            for key in ("filters_json", "sort_json", "pagination_json", "provider_metadata_json"):
                row[key.removesuffix("_json")] = json.loads(row.pop(key))
            row["is_complete"] = bool(row["is_complete"])
        return row

    async def list_search_runs(
        self,
        *,
        study_id: str | None = None,
        topic_id: str | None = None,
        provider: str | None = None,
        mode: str | None = None,
        status: str | None = None,
        search_code: str | None = None,
        label: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("study_id", study_id),
            ("topic_id", topic_id),
            ("provider", provider),
            ("mode", mode),
            ("status", status),
            ("search_code", search_code),
        ):
            if value is not None:
                conditions.append(f"{column}=?")
                parameters.append(value)
        if label:
            conditions.append("label LIKE ?")
            parameters.append(f"%{label}%")
        if date_from:
            conditions.append("executed_at_utc>=?")
            parameters.append(date_from)
        if date_to:
            conditions.append("executed_at_utc<=?")
            parameters.append(date_to)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return await self._fetch_all(
            "SELECT * FROM search_runs" + where + " ORDER BY executed_at_utc DESC LIMIT ? OFFSET ?",
            (*parameters, limit, offset),
        )

    async def list_search_hits(self, search_run_id: str) -> list[dict[str, Any]]:
        rows = await self._fetch_all(
            "SELECT h.search_hit_id,h.search_run_id,h.evidence_id,h.provider,"
            "h.provider_record_id,h.rank,h.discovered_at,e.evidence_code,e.title "
            "FROM search_hits h JOIN evidence e ON e.evidence_id=h.evidence_id "
            "WHERE h.search_run_id=? ORDER BY h.rank",
            (search_run_id,),
        )
        return rows

    async def list_possible_duplicates(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return await self._fetch_all(
            "SELECT d.*,a.evidence_code evidence_code_a,a.title title_a,"
            "b.evidence_code evidence_code_b,b.title title_b "
            "FROM possible_duplicates d "
            "JOIN evidence a ON a.evidence_id=d.evidence_id_a "
            "JOIN evidence b ON b.evidence_id=d.evidence_id_b "
            "ORDER BY d.created_at DESC LIMIT ?",
            (limit,),
        )

    async def create_search_run(
        self,
        *,
        study_id: str,
        topic_id: str | None,
        provider: str,
        mode: SearchMode | str,
        label: str,
        search_intent: str,
        provider_query: str,
        filters: dict[str, Any],
        sort: dict[str, Any],
        requested_limit: int,
    ) -> SearchRun:
        mode_value = SearchMode(mode)
        run_id = str(uuid.uuid4())
        executed = datetime.now(UTC)
        connection = await self._connect()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            row = await (
                await connection.execute(
                    "SELECT COALESCE(MAX(CAST(SUBSTR(search_code, 2) AS INTEGER)), 0) + 1 "
                    "FROM search_runs WHERE study_id = ?",
                    (study_id,),
                )
            ).fetchone()
            search_code = f"Q{int(row[0]):04d}"
            await connection.execute(
                """
                INSERT INTO search_runs(
                    search_run_id,search_code,study_id,topic_id,provider,mode,label,
                    search_intent,provider_query,filters_json,sort_json,executed_at_utc,
                    requested_limit,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'running')
                """,
                (
                    run_id,
                    search_code,
                    study_id,
                    topic_id,
                    provider,
                    mode_value.value,
                    label,
                    search_intent,
                    provider_query,
                    json.dumps(filters, sort_keys=True),
                    json.dumps(sort, sort_keys=True),
                    executed.isoformat(),
                    requested_limit,
                ),
            )
            await connection.commit()
        finally:
            await connection.close()
        return SearchRun(
            search_run_id=run_id,
            search_code=search_code,
            study_id=study_id,
            topic_id=topic_id,
            provider=provider,
            mode=mode_value,
            label=label,
            search_intent=search_intent,
            provider_query=provider_query,
            filters=filters,
            sort=sort,
            requested_limit=requested_limit,
            executed_at_utc=executed,
            status="running",
        )

    async def complete_search_run(
        self,
        search_run_id: str,
        *,
        provider_reported_total: int,
        retrieved_count: int,
        complete: bool,
        pagination: dict[str, Any],
        provider_metadata: dict[str, Any] | None = None,
        duration_ms: int | None = None,
        new_evidence_count: int = 0,
        existing_evidence_count: int = 0,
    ) -> None:
        connection = await self._connect()
        try:
            await connection.execute(
                """
                UPDATE search_runs SET provider_reported_total=?,retrieved_count=?,is_complete=?,
                    pagination_json=?,provider_metadata_json=?,duration_ms=?,new_evidence_count=?,
                    existing_evidence_count=?,status='completed'
                WHERE search_run_id=?
                """,
                (
                    provider_reported_total,
                    retrieved_count,
                    int(complete),
                    json.dumps(pagination, sort_keys=True),
                    json.dumps(provider_metadata or {}, sort_keys=True),
                    duration_ms,
                    new_evidence_count,
                    existing_evidence_count,
                    search_run_id,
                ),
            )
            await connection.commit()
        finally:
            await connection.close()

    async def fail_search_run(
        self,
        search_run_id: str,
        error_type: str,
        error_summary: str,
        duration_ms: int,
        *,
        retrieved_count: int = 0,
        new_evidence_count: int = 0,
        existing_evidence_count: int = 0,
    ) -> None:
        connection = await self._connect()
        try:
            await connection.execute(
                "UPDATE search_runs SET status=?,error_type=?,error_summary=?,duration_ms=?,"
                "retrieved_count=?,new_evidence_count=?,existing_evidence_count=?,is_complete=0 "
                "WHERE search_run_id=?",
                (
                    "partial" if retrieved_count else "failed",
                    error_type,
                    error_summary,
                    duration_ms,
                    retrieved_count,
                    new_evidence_count,
                    existing_evidence_count,
                    search_run_id,
                ),
            )
            await connection.commit()
        finally:
            await connection.close()

    async def ingest_search_hit(
        self, search_run_id: str, rank: int, record: SourceRecord
    ) -> IngestResult:
        connection = await self._connect()
        now = _utc_text()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            doi = normalize_doi(record.doi or record.identifiers.get("doi"))
            evidence_id: str | None = None
            matched_by: str | None = None
            if doi:
                row = await (
                    await connection.execute(
                        "SELECT evidence_id FROM evidence WHERE normalized_doi=?", (doi,)
                    )
                ).fetchone()
                if row:
                    evidence_id, matched_by = str(row[0]), "doi"
            if evidence_id is None:
                for identifier_type, identifier_value in record.identifiers.items():
                    normalized_value = normalize_text(identifier_value)
                    row = await (
                        await connection.execute(
                            "SELECT evidence_id FROM evidence_identifiers "
                            "WHERE identifier_type=? AND normalized_value=?",
                            (identifier_type, normalized_value),
                        )
                    ).fetchone()
                    if row:
                        evidence_id, matched_by = str(row[0]), f"identifier:{identifier_type}"
                        break
            fingerprint = bibliographic_fingerprint(record)
            possible_duplicate_ids: list[str] = []
            if evidence_id is None and fingerprint:
                rows = await (
                    await connection.execute(
                        "SELECT evidence_id,normalized_doi FROM evidence "
                        "WHERE bibliographic_fingerprint=?",
                        (fingerprint,),
                    )
                ).fetchall()
                if len(rows) == 1:
                    existing_doi = rows[0]["normalized_doi"]
                    if not doi or not existing_doi or doi == existing_doi:
                        evidence_id = str(rows[0]["evidence_id"])
                        matched_by = "bibliographic_fingerprint"
                    else:
                        possible_duplicate_ids.append(str(rows[0]["evidence_id"]))
            if evidence_id is None and record.year and record.title and record.authors:
                first_author = normalize_text(str(record.authors[0].get("name") or ""))
                candidates = await (
                    await connection.execute(
                        "SELECT evidence_id,normalized_title,author_names "
                        "FROM evidence WHERE year=?",
                        (record.year,),
                    )
                ).fetchall()
                normalized_title = normalize_text(record.title)
                for candidate in candidates:
                    if normalize_text(candidate["author_names"].split(";", 1)[0]) != first_author:
                        continue
                    similarity = SequenceMatcher(
                        None, normalized_title, candidate["normalized_title"]
                    ).ratio()
                    if 0.65 <= similarity < 1:
                        possible_duplicate_ids.append(str(candidate["evidence_id"]))
            created = evidence_id is None
            if created:
                evidence_id = str(uuid.uuid4())
                row = await (
                    await connection.execute("SELECT COUNT(*) + 1 FROM evidence")
                ).fetchone()
                evidence_code = f"E{int(row[0]):06d}"
                authors_json = json.dumps(record.authors, ensure_ascii=False, sort_keys=True)
                author_names = "; ".join(str(author.get("name") or "") for author in record.authors)
                await connection.execute(
                    """
                    INSERT INTO evidence(
                        evidence_id,evidence_code,title,normalized_title,authors_json,author_names,
                        abstract,year,publication_date,publication,doi,normalized_doi,url,
                        document_type,publication_type,review_status,keywords_json,
                        citation_count,citation_source,
                        citation_timestamp,open_access_json,bibliographic_fingerprint,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        evidence_id,
                        evidence_code,
                        record.title,
                        normalize_text(record.title),
                        authors_json,
                        author_names,
                        record.abstract,
                        record.year,
                        record.publication_date,
                        record.publication,
                        record.doi,
                        doi,
                        record.url,
                        record.document_type,
                        record.publication_type,
                        record.review_status,
                        json.dumps(record.keywords, ensure_ascii=False),
                        record.citation_count,
                        record.provider if record.citation_count is not None else None,
                        now if record.citation_count is not None else None,
                        json.dumps(record.open_access) if record.open_access else None,
                        fingerprint,
                        now,
                        now,
                    ),
                )
                for candidate_id in set(possible_duplicate_ids):
                    evidence_id_a, evidence_id_b = sorted((evidence_id, candidate_id))
                    await connection.execute(
                        "INSERT OR IGNORE INTO possible_duplicates VALUES(?,?,?,?,?,?)",
                        (
                            str(uuid.uuid4()),
                            evidence_id_a,
                            evidence_id_b,
                            "similar_bibliographic_metadata_requires_review",
                            "open",
                            now,
                        ),
                    )
            identifiers = dict(record.identifiers)
            if doi:
                identifiers["doi"] = doi
            for identifier_type, identifier_value in identifiers.items():
                if not identifier_value:
                    continue
                await connection.execute(
                    """
                    INSERT OR IGNORE INTO evidence_identifiers(
                        evidence_id,identifier_type,identifier_value,normalized_value,
                        source_provider,created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        evidence_id,
                        identifier_type,
                        identifier_value,
                        normalize_text(identifier_value),
                        record.provider,
                        now,
                    ),
                )
            normalized = record.model_dump(mode="json", exclude={"raw_metadata"})
            await connection.execute(
                """
                INSERT INTO search_hits(
                    search_hit_id,search_run_id,evidence_id,provider,provider_record_id,rank,
                    normalized_json,raw_metadata_json,discovered_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(uuid.uuid4()),
                    search_run_id,
                    evidence_id,
                    record.provider,
                    record.provider_record_id,
                    rank,
                    json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                    json.dumps(record.raw_metadata, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            row = await (
                await connection.execute(
                    "SELECT title,abstract,author_names,publication,keywords_json FROM evidence "
                    "WHERE evidence_id=?",
                    (evidence_id,),
                )
            ).fetchone()
            await connection.execute("DELETE FROM evidence_fts WHERE evidence_id=?", (evidence_id,))
            await connection.execute(
                "INSERT INTO evidence_fts(evidence_id,title,abstract,authors,publication,keywords) "
                "VALUES(?,?,?,?,?,?)",
                (
                    evidence_id,
                    row["title"] or "",
                    row["abstract"] or "",
                    row["author_names"] or "",
                    row["publication"] or "",
                    row["keywords_json"] or "",
                ),
            )
            await connection.commit()
            return IngestResult(evidence_id=evidence_id, created=created, matched_by=matched_by)
        except Exception:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    async def list_discoveries(self, evidence_id: str) -> list[dict[str, Any]]:
        connection = await self._connect()
        try:
            rows = await (
                await connection.execute(
                    """
                    SELECT h.provider,h.provider_record_id,h.rank,h.discovered_at,
                           r.search_run_id,r.search_code,r.study_id,r.topic_id,r.mode,r.label,
                           r.search_intent,r.provider_query,r.executed_at_utc
                    FROM search_hits h JOIN search_runs r ON r.search_run_id=h.search_run_id
                    WHERE h.evidence_id=? ORDER BY r.executed_at_utc,h.rank
                    """,
                    (evidence_id,),
                )
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            await connection.close()

    async def set_screening(
        self,
        evidence_id: str,
        status: ScreeningStatus | str,
        *,
        reason: str | None,
        note: str | None,
        actor: str,
    ) -> None:
        new_status = ScreeningStatus(status)
        connection = await self._connect()
        now = _utc_text()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            row = await (
                await connection.execute(
                    "SELECT screening_status FROM evidence WHERE evidence_id=?", (evidence_id,)
                )
            ).fetchone()
            if not row:
                raise KeyError(evidence_id)
            old_status = str(row[0])
            await connection.execute(
                "UPDATE evidence SET screening_status=?,exclusion_reason=?,"
                "final_corpus=?,updated_at=? "
                "WHERE evidence_id=?",
                (
                    new_status.value,
                    reason if new_status is ScreeningStatus.EXCLUDED else None,
                    int(new_status is ScreeningStatus.FINAL),
                    now,
                    evidence_id,
                ),
            )
            await connection.execute(
                "INSERT INTO screening_events VALUES(?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    evidence_id,
                    now,
                    old_status,
                    new_status.value,
                    reason,
                    note,
                    actor,
                ),
            )
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    async def screening_history(self, evidence_id: str) -> list[dict[str, Any]]:
        connection = await self._connect()
        try:
            rows = await (
                await connection.execute(
                    "SELECT * FROM screening_events WHERE evidence_id=? ORDER BY timestamp_utc",
                    (evidence_id,),
                )
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            await connection.close()

    async def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        row = await self._fetch_one("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,))
        if not row:
            return None
        row["authors"] = json.loads(row.pop("authors_json"))
        row["keywords"] = json.loads(row.pop("keywords_json"))
        row["open_access"] = (
            json.loads(row.pop("open_access_json")) if row.get("open_access_json") else None
        )
        row["final_corpus"] = bool(row["final_corpus"])
        row["identifiers"] = await self._fetch_all(
            "SELECT identifier_type,identifier_value,source_provider FROM evidence_identifiers "
            "WHERE evidence_id=? ORDER BY identifier_type",
            (evidence_id,),
        )
        row["discoveries"] = await self.list_discoveries(evidence_id)
        return row

    async def add_note(self, evidence_id: str, text: str, actor: str) -> dict[str, Any]:
        note_id = str(uuid.uuid4())
        now = _utc_text()
        connection = await self._connect()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "INSERT INTO notes(note_id,evidence_id,text,actor,created_at) VALUES(?,?,?,?,?)",
                (note_id, evidence_id, text, actor, now),
            )
            await connection.execute(
                "UPDATE evidence SET notes_summary=?,updated_at=? WHERE evidence_id=?",
                (text[:500], now, evidence_id),
            )
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise
        finally:
            await connection.close()
        return {"note_id": note_id, "evidence_id": evidence_id, "text": text, "actor": actor}

    async def list_notes(self, evidence_id: str) -> list[dict[str, Any]]:
        return await self._fetch_all(
            "SELECT * FROM notes WHERE evidence_id=? ORDER BY created_at", (evidence_id,)
        )

    async def audit(
        self,
        operation: str,
        *,
        status: str,
        study_id: str | None = None,
        topic_id: str | None = None,
        source: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        duration_ms: int | None = None,
        safe_summary: str | None = None,
        error_type: str | None = None,
    ) -> str:
        audit_event_id = str(uuid.uuid4())
        connection = await self._connect()
        try:
            await connection.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    audit_event_id,
                    _utc_text(),
                    study_id,
                    topic_id,
                    operation,
                    source,
                    entity_type,
                    entity_id,
                    status,
                    duration_ms,
                    safe_summary,
                    error_type,
                ),
            )
            await connection.commit()
            return audit_event_id
        finally:
            await connection.close()

    async def list_audit_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return await self._fetch_all(
            "SELECT * FROM audit_events ORDER BY timestamp_utc DESC LIMIT ?", (limit,)
        )

    async def get_zotero_link(
        self, evidence_id: str, library_type: str, library_id: str
    ) -> dict[str, Any] | None:
        return await self._fetch_one(
            "SELECT * FROM zotero_links WHERE evidence_id=? AND library_type=? AND library_id=?",
            (evidence_id, library_type, library_id),
        )

    async def save_zotero_link(
        self,
        evidence_id: str,
        library_type: str,
        library_id: str,
        item_key: str,
    ) -> None:
        connection = await self._connect()
        try:
            await connection.execute(
                "INSERT INTO zotero_links VALUES(?,?,?,?,?) "
                "ON CONFLICT(evidence_id,library_type,library_id) DO UPDATE SET "
                "item_key=excluded.item_key,synced_at=excluded.synced_at",
                (evidence_id, library_type, library_id, item_key, _utc_text()),
            )
            await connection.commit()
        finally:
            await connection.close()

    async def get_zotero_link_by_item_key(
        self, item_key: str, library_type: str, library_id: str
    ) -> dict[str, Any] | None:
        return await self._fetch_one(
            "SELECT * FROM zotero_links WHERE item_key=? AND library_type=? AND library_id=? "
            "ORDER BY synced_at DESC LIMIT 1",
            (item_key, library_type, library_id),
        )

    async def delete_zotero_links_by_item_key(
        self, item_key: str, library_type: str, library_id: str
    ) -> int:
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                "DELETE FROM zotero_links WHERE item_key=? AND library_type=? AND library_id=?",
                (item_key, library_type, library_id),
            )
            await connection.commit()
            return int(cursor.rowcount)
        finally:
            await connection.close()

    async def save_citation_reference(
        self,
        *,
        manuscript: str,
        citation_location: str | None,
        library_type: str,
        library_id: str,
        item_key: str,
        evidence_id: str | None,
        identifier: str | None,
        rationale: str = "",
    ) -> dict[str, Any]:
        citation_reference_id = str(uuid.uuid4())
        created_at = _utc_text()
        connection = await self._connect()
        try:
            await connection.execute(
                "INSERT INTO citation_references VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    citation_reference_id,
                    manuscript,
                    citation_location,
                    library_type,
                    library_id,
                    item_key,
                    evidence_id,
                    identifier,
                    rationale,
                    created_at,
                ),
            )
            await connection.commit()
        finally:
            await connection.close()
        return {
            "citation_reference_id": citation_reference_id,
            "manuscript": manuscript,
            "citation_location": citation_location,
            "library_type": library_type,
            "library_id": library_id,
            "item_key": item_key,
            "evidence_id": evidence_id,
            "identifier": identifier,
            "rationale": rationale,
            "created_at": created_at,
        }

    async def list_citation_references(
        self, *, manuscript: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if manuscript:
            return await self._fetch_all(
                "SELECT * FROM citation_references WHERE manuscript=? "
                "ORDER BY created_at DESC LIMIT ?",
                (manuscript, limit),
            )
        return await self._fetch_all(
            "SELECT * FROM citation_references ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    async def record_github_operation(
        self,
        operation: str,
        repository: str,
        *,
        dry_run: bool,
        status: str,
        safe_summary: str,
    ) -> str:
        operation_id = str(uuid.uuid4())
        connection = await self._connect()
        try:
            await connection.execute(
                "INSERT INTO github_operations VALUES(?,?,?,?,?,?,?)",
                (
                    operation_id,
                    operation,
                    repository,
                    int(dry_run),
                    status,
                    safe_summary,
                    _utc_text(),
                ),
            )
            await connection.commit()
            return operation_id
        finally:
            await connection.close()

    async def summary(self, study_id: str | None = None) -> dict[str, int]:
        connection = await self._connect()
        try:
            if study_id:
                evidence_where = (
                    " WHERE EXISTS (SELECT 1 FROM search_hits h JOIN search_runs r "
                    "ON r.search_run_id=h.search_run_id WHERE h.evidence_id=e.evidence_id "
                    "AND r.study_id=?)"
                )
                args: tuple[Any, ...] = (study_id,)
            else:
                evidence_where, args = "", ()
            row = await (
                await connection.execute(
                    "SELECT COUNT(*) total,"
                    "SUM(screening_status='unreviewed') unreviewed,"
                    "SUM(screening_status='included') included,"
                    "SUM(screening_status='excluded') excluded,"
                    "SUM(final_corpus=1) final_count FROM evidence e" + evidence_where,
                    args,
                )
            ).fetchone()
            result = {key: int(row[key] or 0) for key in row.keys()}  # noqa: SIM118
            run_row = await (
                await connection.execute(
                    "SELECT COUNT(*) FROM search_runs" + (" WHERE study_id=?" if study_id else ""),
                    args,
                )
            ).fetchone()
            result["search_runs"] = int(run_row[0])
            return result
        finally:
            await connection.close()

    async def list_evidence(
        self,
        *,
        study_id: str | None = None,
        topic_id: str | None = None,
        provider: str | None = None,
        search_code: str | None = None,
        status: str | None = None,
        final: bool | None = None,
        query: str | None = None,
        year: int | None = None,
        document_type: str | None = None,
        publication_type: str | None = None,
        review_status: str | None = None,
        discovered_from: str | None = None,
        discovered_to: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> Page:
        conditions: list[str] = []
        parameters: list[Any] = []
        if study_id:
            conditions.append("r.study_id=?")
            parameters.append(study_id)
        if topic_id:
            conditions.append("r.topic_id=?")
            parameters.append(topic_id)
        if provider:
            conditions.append("h.provider=?")
            parameters.append(provider)
        if search_code:
            conditions.append("r.search_code=?")
            parameters.append(search_code)
        if status:
            conditions.append("e.screening_status=?")
            parameters.append(status)
        if final is not None:
            conditions.append("e.final_corpus=?")
            parameters.append(int(final))
        if query:
            conditions.append(
                "(e.title LIKE ? OR e.author_names LIKE ? OR e.publication LIKE ? OR e.doi LIKE ?)"
            )
            like = f"%{query}%"
            parameters.extend([like, like, like, like])
        if year is not None:
            conditions.append("e.year=?")
            parameters.append(year)
        if document_type:
            conditions.append("e.document_type=?")
            parameters.append(document_type)
        if publication_type:
            conditions.append("e.publication_type=?")
            parameters.append(publication_type)
        if review_status:
            conditions.append("e.review_status=?")
            parameters.append(review_status)
        if discovered_from:
            conditions.append("h.discovered_at>=?")
            parameters.append(discovered_from)
        if discovered_to:
            conditions.append("h.discovered_at<=?")
            parameters.append(discovered_to)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        base = (
            " FROM evidence e LEFT JOIN search_hits h ON h.evidence_id=e.evidence_id "
            "LEFT JOIN search_runs r ON r.search_run_id=h.search_run_id"
        )
        connection = await self._connect()
        try:
            count_row = await (
                await connection.execute(
                    "SELECT COUNT(DISTINCT e.evidence_id)" + base + where, parameters
                )
            ).fetchone()
            rows = await (
                await connection.execute(
                    "SELECT DISTINCT e.*"
                    + base
                    + where
                    + " ORDER BY e.created_at DESC LIMIT ? OFFSET ?",
                    [*parameters, limit, offset],
                )
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["authors"] = json.loads(item.pop("authors_json"))
                item["keywords"] = json.loads(item.pop("keywords_json"))
                item["final_corpus"] = bool(item["final_corpus"])
                items.append(item)
            return Page(items=items, total=int(count_row[0]), offset=offset, limit=limit)
        finally:
            await connection.close()

    async def _fetch_one(self, sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        connection = await self._connect()
        try:
            row = await (await connection.execute(sql, parameters)).fetchone()
            return dict(row) if row else None
        finally:
            await connection.close()

    async def _fetch_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        connection = await self._connect()
        try:
            rows = await (await connection.execute(sql, parameters)).fetchall()
            return [dict(row) for row in rows]
        finally:
            await connection.close()

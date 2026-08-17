from __future__ import annotations

import time
from typing import Any

from research_gateway.db.database import EvidenceDatabase
from research_gateway.domain.models import SearchMode, SourceRecord
from research_gateway.security import redact_text
from research_gateway.sources.base import ProviderError, SourceAdapter
from research_gateway.sources.registry import SourceRegistry


class ResearchService:
    """Coordinates source calls and records their durable, auditable outcome."""

    def __init__(self, database: EvidenceDatabase, sources: SourceRegistry) -> None:
        self.database = database
        self.sources = sources

    async def explore(
        self,
        *,
        study_id: str,
        topic_id: str | None,
        provider: str,
        search_intent: str,
        provider_query: str,
        label: str = "",
        filters: dict[str, Any] | None = None,
        sort: dict[str, Any] | None = None,
        requested_limit: int = 0,
    ) -> dict[str, Any]:
        """Count a query and record it without creating hits or evidence."""
        adapter = self._available(provider)
        filters = filters or {}
        sort = sort or {}
        started = time.perf_counter()
        run = await self.database.create_search_run(
            study_id=study_id,
            topic_id=topic_id,
            provider=provider,
            mode=SearchMode.EXPLORE,
            label=label,
            search_intent=search_intent,
            provider_query=provider_query,
            filters=filters,
            sort=sort,
            requested_limit=requested_limit,
        )
        try:
            total = await adapter.count(provider_query, filters=filters)
            duration_ms = _duration_ms(started)
            await self.database.complete_search_run(
                run.search_run_id,
                provider_reported_total=total,
                retrieved_count=0,
                complete=True,
                pagination={"operation": "count"},
                duration_ms=duration_ms,
            )
            await self.database.audit(
                "search.explore",
                status="completed",
                study_id=study_id,
                topic_id=topic_id,
                source=provider,
                entity_type="search_run",
                entity_id=run.search_run_id,
                duration_ms=duration_ms,
                safe_summary=f"Provider reported {total} matching records; no hits were saved.",
            )
            return {
                **run.model_dump(mode="json"),
                "status": "completed",
                "provider_reported_total": total,
                "retrieved_count": 0,
                "new_evidence_count": 0,
                "existing_evidence_count": 0,
            }
        except Exception as exc:
            await self._record_failure(
                run.search_run_id, started, exc, study_id, topic_id, provider
            )
            raise

    async def save(
        self,
        *,
        study_id: str,
        topic_id: str | None,
        provider: str,
        search_intent: str,
        provider_query: str,
        requested_limit: int,
        label: str = "",
        filters: dict[str, Any] | None = None,
        sort: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run the exact provider query and preserve each permitted discovery."""
        if requested_limit < 1:
            raise ValueError("requested_limit must be at least 1 in save mode")
        adapter = self._available(provider)
        filters = filters or {}
        sort = sort or {}
        started = time.perf_counter()
        run = await self.database.create_search_run(
            study_id=study_id,
            topic_id=topic_id,
            provider=provider,
            mode=SearchMode.SAVE,
            label=label,
            search_intent=search_intent,
            provider_query=provider_query,
            filters=filters,
            sort=sort,
            requested_limit=requested_limit,
        )
        retrieved = created = existing = 0
        total = 0
        offset = 0
        last_page: dict[str, Any] = {}
        try:
            while retrieved < requested_limit:
                page_limit = min(
                    adapter.retention_policy.max_page_size,
                    requested_limit - retrieved,
                )
                page = await adapter.search(
                    provider_query,
                    limit=page_limit,
                    offset=offset,
                    filters=filters,
                    sort=sort,
                )
                total = page.total_results
                last_page = page.pagination
                if not page.records:
                    break
                for record in page.records:
                    retained = _apply_retention(record, adapter)
                    outcome = await self.database.ingest_search_hit(
                        run.search_run_id, retrieved + 1, retained
                    )
                    retrieved += 1
                    created += int(outcome.created)
                    existing += int(not outcome.created)
                    if retrieved >= requested_limit:
                        break
                if page.next_offset is None or page.next_offset <= offset:
                    break
                offset = page.next_offset
            complete = retrieved >= total
            duration_ms = _duration_ms(started)
            await self.database.complete_search_run(
                run.search_run_id,
                provider_reported_total=total,
                retrieved_count=retrieved,
                complete=complete,
                pagination={**last_page, "requested_limit": requested_limit},
                provider_metadata={
                    "retention_policy": adapter.retention_policy.model_dump(mode="json")
                },
                duration_ms=duration_ms,
                new_evidence_count=created,
                existing_evidence_count=existing,
            )
            await self.database.audit(
                "search.save",
                status="completed",
                study_id=study_id,
                topic_id=topic_id,
                source=provider,
                entity_type="search_run",
                entity_id=run.search_run_id,
                duration_ms=duration_ms,
                safe_summary=(
                    f"Saved {retrieved} discoveries: {created} new evidence records and "
                    f"{existing} existing records."
                ),
            )
            return {
                **run.model_dump(mode="json"),
                "status": "completed",
                "provider_reported_total": total,
                "retrieved_count": retrieved,
                "is_complete": complete,
                "new_evidence_count": created,
                "existing_evidence_count": existing,
            }
        except Exception as exc:
            await self._record_failure(
                run.search_run_id,
                started,
                exc,
                study_id,
                topic_id,
                provider,
                retrieved_count=retrieved,
                new_evidence_count=created,
                existing_evidence_count=existing,
            )
            raise

    def _available(self, provider: str) -> SourceAdapter:
        adapter = self.sources.get(provider)
        if not adapter.status.available:
            reason = adapter.status.unavailable_reason or "not_available"
            raise RuntimeError(f"Research source {provider} is unavailable: {reason}")
        return adapter

    async def _record_failure(
        self,
        run_id: str,
        started: float,
        exc: Exception,
        study_id: str,
        topic_id: str | None,
        provider: str,
        retrieved_count: int = 0,
        new_evidence_count: int = 0,
        existing_evidence_count: int = 0,
    ) -> None:
        duration_ms = _duration_ms(started)
        error_type = exc.error_type if isinstance(exc, ProviderError) else type(exc).__name__
        summary = redact_text(
            exc.safe_message if isinstance(exc, ProviderError) else "Search operation failed."
        )
        await self.database.fail_search_run(
            run_id,
            error_type,
            summary,
            duration_ms,
            retrieved_count=retrieved_count,
            new_evidence_count=new_evidence_count,
            existing_evidence_count=existing_evidence_count,
        )
        await self.database.audit(
            "search.execute",
            status="failed",
            study_id=study_id,
            topic_id=topic_id,
            source=provider,
            entity_type="search_run",
            entity_id=run_id,
            duration_ms=duration_ms,
            safe_summary=summary,
            error_type=error_type,
        )


def _apply_retention(record: SourceRecord, adapter: SourceAdapter) -> SourceRecord:
    retained = record.model_copy(deep=True)
    policy = adapter.retention_policy
    if policy.abstract_storage == "restricted":
        retained.abstract = None
    if policy.raw_metadata == "none":
        retained.raw_metadata = {}
    elif policy.raw_metadata == "minimal":
        retained.raw_metadata = {
            key: value
            for key, value in retained.raw_metadata.items()
            if key in {"eid", "scopus_id", "article_number", "uid", "subtype"}
        }
    return retained


def _duration_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))

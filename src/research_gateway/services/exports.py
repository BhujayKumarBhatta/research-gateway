from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Literal

from openpyxl import Workbook

from research_gateway.db.database import EvidenceDatabase

ExportFormat = Literal["json", "csv", "xlsx", "markdown"]


class ExportService:
    def __init__(self, database: EvidenceDatabase) -> None:
        self.database = database

    async def export(
        self,
        target: Path,
        *,
        format: ExportFormat,
        study_id: str | None = None,
        topic_id: str | None = None,
        final_only: bool = False,
    ) -> dict[str, Any]:
        target = target.expanduser().absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        page = await self.database.list_evidence(
            study_id=study_id,
            topic_id=topic_id,
            final=True if final_only else None,
            limit=100_000,
        )
        evidence = []
        discoveries = []
        screening = []
        for item in page.items:
            details = await self.database.get_evidence(item["evidence_id"])
            if details:
                paths = details.pop("discoveries")
                discoveries.extend(paths)
                details["first_discovery"] = paths[0]["executed_at_utc"] if paths else None
                details["last_discovery"] = paths[-1]["executed_at_utc"] if paths else None
                evidence.append(details)
                screening.extend(await self.database.screening_history(item["evidence_id"]))
        runs = await self.database.list_search_runs(study_id=study_id, limit=100_000)
        topics = await self.database.list_topics(study_id) if study_id else []
        if format == "json":
            target.write_text(
                json.dumps(
                    {
                        "evidence": evidence,
                        "discoveries": discoveries,
                        "search_runs": runs,
                        "screening": screening,
                        "topics": topics,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        elif format == "csv":
            _write_csv(target, evidence)
        elif format == "xlsx":
            _write_xlsx(target, evidence, discoveries, runs, screening, topics)
        elif format == "markdown":
            _write_markdown(target, evidence)
        else:
            raise ValueError(f"Unknown export format: {format}")
        await self.database.audit(
            "export.create",
            status="completed",
            study_id=study_id,
            entity_type="file",
            entity_id=str(target),
            safe_summary=f"Exported {len(evidence)} evidence records as {format}.",
        )
        return {
            "path": str(target),
            "format": format,
            "evidence_count": len(evidence),
            "discovery_count": len(discoveries),
            "search_run_count": len(runs),
        }


def _flat(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_code": item.get("evidence_code"),
        "title": item.get("title"),
        "authors": "; ".join(a.get("name", "") for a in item.get("authors") or []),
        "year": item.get("year"),
        "publication": item.get("publication"),
        "doi": item.get("normalized_doi") or item.get("doi"),
        "url": item.get("url"),
        "screening_status": item.get("screening_status"),
        "final_corpus": item.get("final_corpus"),
        "abstract": item.get("abstract"),
        "keywords": "; ".join(item.get("keywords") or []),
        "document_type": item.get("document_type"),
        "citation_count": item.get("citation_count"),
        "exclusion_reason": item.get("exclusion_reason"),
        "notes": item.get("notes_summary"),
        "first_discovery": item.get("first_discovery"),
        "last_discovery": item.get("last_discovery"),
    }


def _write_csv(path: Path, evidence: list[dict[str, Any]]) -> None:
    fields = list(_flat({}).keys())
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(_flat(item) for item in evidence)


def _write_xlsx(
    path: Path,
    evidence: list[dict[str, Any]],
    discoveries: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    screening: list[dict[str, Any]],
    topics: list[dict[str, Any]],
) -> None:
    workbook = Workbook()
    default = workbook.active
    workbook.remove(default)
    for name, rows in (
        ("Evidence", [_flat(item) for item in evidence]),
        ("Discoveries", discoveries),
        ("Search Runs", runs),
        ("Screening", screening),
        ("Topics", topics),
    ):
        sheet = workbook.create_sheet(name)
        keys = list(rows[0].keys()) if rows else ["empty"]
        sheet.append(keys)
        for row in rows:
            sheet.append([_cell(row.get(key)) for key in keys])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
    workbook.save(path)


def _cell(value: Any) -> Any:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        if isinstance(value, (dict, list))
        else value
    )


def _write_markdown(path: Path, evidence: list[dict[str, Any]]) -> None:
    lines = [
        "# Research evidence",
        "",
        "| Code | Title | Year | Status | DOI |",
        "|---|---|---:|---|---|",
    ]
    for item in evidence:
        row = _flat(item)
        values = [
            row["evidence_code"],
            row["title"],
            row["year"],
            row["screening_status"],
            row["doi"],
        ]
        lines.append(
            "| " + " | ".join(str(value or "").replace("|", "\\|") for value in values) + " |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

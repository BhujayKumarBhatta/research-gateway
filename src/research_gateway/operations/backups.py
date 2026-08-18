from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from research_gateway.db.database import EvidenceDatabase
from research_gateway.services.exports import ExportService


@dataclass(frozen=True)
class BackupResult:
    path: Path
    latest_path: Path
    evidence_count: int


class ExcelBackupService:
    """Create recoverable Excel snapshots while SQLite remains the master record."""

    def __init__(
        self,
        database: EvidenceDatabase,
        directory: Path | None = None,
        *,
        retention_count: int = 20,
    ) -> None:
        self.database = database
        self.directory = (directory or database.path.parent / "backups").expanduser().absolute()
        self.retention_count = max(1, retention_count)

    async def create(self) -> BackupResult:
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.directory / f"research-gateway-{stamp}.xlsx"
        exported = await ExportService(self.database).export(target, format="xlsx")
        latest = self.directory / "latest.xlsx"
        shutil.copy2(target, latest)
        self._prune()
        return BackupResult(
            path=target,
            latest_path=latest,
            evidence_count=int(exported["evidence_count"]),
        )

    def _prune(self) -> None:
        snapshots = sorted(
            self.directory.glob("research-gateway-*.xlsx"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for stale in snapshots[self.retention_count :]:
            stale.unlink(missing_ok=True)

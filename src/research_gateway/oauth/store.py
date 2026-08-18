from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any


class OAuthStore:
    """Durable OAuth state containing keyed digests, never raw grants or tokens."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_records (
                    kind TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL,
                    revoked_at REAL,
                    family_id TEXT,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (kind, digest)
                );
                CREATE INDEX IF NOT EXISTS oauth_records_family
                    ON oauth_records(family_id);
                """
            )
        with suppress(OSError):
            self.path.chmod(0o600)

    def put_client(self, client_id: str, payload: dict[str, Any]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO oauth_clients(client_id, payload_json, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET payload_json=excluded.payload_json
                """,
                (client_id, json.dumps(payload, separators=(",", ":")), time.time()),
            )

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM oauth_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def put_record(
        self,
        kind: str,
        digest: str,
        payload: dict[str, Any],
        expires_at: float,
        *,
        family_id: str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO oauth_records(
                    kind, digest, payload_json, expires_at, family_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    digest,
                    json.dumps(payload, separators=(",", ":")),
                    expires_at,
                    family_id,
                    time.time(),
                ),
            )

    def get_record(
        self, kind: str, digest: str, *, include_inactive: bool = False
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM oauth_records WHERE kind = ? AND digest = ?", (kind, digest)
            ).fetchone()
        if not row:
            return None
        if not include_inactive and (
            row["used_at"] is not None
            or row["revoked_at"] is not None
            or row["expires_at"] < time.time()
        ):
            return None
        payload = json.loads(row["payload_json"])
        payload["_used_at"] = row["used_at"]
        payload["_revoked_at"] = row["revoked_at"]
        payload["_family_id"] = row["family_id"]
        return payload

    def consume_record(self, kind: str, digest: str) -> dict[str, Any] | None:
        now = time.time()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM oauth_records
                WHERE kind = ? AND digest = ? AND used_at IS NULL
                  AND revoked_at IS NULL AND expires_at >= ?
                """,
                (kind, digest, now),
            ).fetchone()
            if not row:
                return None
            updated = connection.execute(
                """
                UPDATE oauth_records SET used_at = ?
                WHERE kind = ? AND digest = ? AND used_at IS NULL AND revoked_at IS NULL
                """,
                (now, kind, digest),
            )
            if updated.rowcount != 1:
                return None
        payload = json.loads(row["payload_json"])
        payload["_family_id"] = row["family_id"]
        return payload

    def revoke_family(self, family_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE oauth_records SET revoked_at = ?
                WHERE family_id = ? AND revoked_at IS NULL
                """,
                (time.time(), family_id),
            )

    def revoke_record(self, kind: str, digest: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE oauth_records SET revoked_at = ?
                WHERE kind = ? AND digest = ? AND revoked_at IS NULL
                """,
                (time.time(), kind, digest),
            )

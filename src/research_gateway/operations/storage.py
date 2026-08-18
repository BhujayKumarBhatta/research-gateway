from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tomllib
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from research_gateway.config import load_settings


@dataclass(frozen=True)
class StorageRelocationResult:
    database_path: Path
    log_path: Path
    backup_directory: Path
    runtime_directory: Path
    config_backup: Path
    database_copied: bool


def relocate_storage(config_path: Path, root: Path) -> StorageRelocationResult:
    """Move mutable application data while preserving the source DB and all secrets."""
    selected = config_path.expanduser().absolute()
    destination = root.expanduser().absolute()
    settings = load_settings(selected, require_file=True)

    data_directory = destination / "data"
    log_path = destination / "logs" / "research-gateway.log"
    backup_directory = destination / "backups"
    runtime_directory = destination / "runtime"
    database_path = data_directory / "research_gateway.db"
    for directory in (
        data_directory,
        log_path.parent,
        backup_directory,
        runtime_directory,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    source = settings.database.path
    copied = False
    if source != database_path and source.is_file() and not database_path.exists():
        with (
            sqlite3.connect(source) as source_connection,
            sqlite3.connect(database_path) as target_connection,
        ):
            source_connection.backup(target_connection)
        copied = True
    elif not database_path.exists():
        with sqlite3.connect(database_path):
            pass
    else:
        with sqlite3.connect(database_path) as connection:
            connection.execute("PRAGMA schema_version").fetchone()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    config_backup = selected.with_name(f"{selected.name}.backup-{stamp}")
    shutil.copy2(selected, config_backup)

    text = selected.read_text(encoding="utf-8")
    updates = {
        ("database", "path"): database_path,
        ("logging", "path"): log_path,
        ("backup", "directory"): backup_directory,
        ("runtime", "directory"): runtime_directory,
    }
    for (section, key), value in updates.items():
        text = _set_toml_string(text, section, key, str(value))

    temporary = selected.with_name(f".{selected.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        with temporary.open("rb") as stream:
            tomllib.load(stream)
        with suppress(OSError):
            temporary.chmod(0o600)
        os.replace(temporary, selected)
    finally:
        if temporary.exists():
            temporary.unlink()

    return StorageRelocationResult(
        database_path=database_path,
        log_path=log_path,
        backup_directory=backup_directory,
        runtime_directory=runtime_directory,
        config_backup=config_backup,
        database_copied=copied,
    )


def _set_toml_string(text: str, section: str, key: str, value: str) -> str:
    lines = text.splitlines()
    header = f"[{section}]"
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == header)
    except StopIteration:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([header, f"{key} = {json.dumps(value)}"])
        return "\n".join(lines) + "\n"

    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.fullmatch(r"\s*\[[^]]+]\s*", lines[index])
        ),
        len(lines),
    )
    key_pattern = re.compile(rf"\s*{re.escape(key)}\s*=")
    for index in range(start + 1, end):
        if key_pattern.match(lines[index]):
            lines[index] = f"{key} = {json.dumps(value)}"
            return "\n".join(lines) + "\n"
    lines.insert(end, f"{key} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"

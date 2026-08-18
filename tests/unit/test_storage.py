from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

from research_gateway.operations.storage import relocate_storage


def test_relocate_storage_copies_database_and_preserves_credentials(tmp_path: Path) -> None:
    old_database = tmp_path / "old" / "research_gateway.db"
    old_database.parent.mkdir()
    with sqlite3.connect(old_database) as connection:
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES('preserved')")

    config = tmp_path / "config.toml"
    config.write_text(
        f'''[database]\npath = "{old_database}"\n\n[scopus]\napi_key = "test-secret"\n''',
        encoding="utf-8",
    )
    root = tmp_path / "D-drive" / "research-gateway"

    result = relocate_storage(config, root)

    target = root / "data" / "research_gateway.db"
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("preserved",)
    with config.open("rb") as stream:
        payload = tomllib.load(stream)
    assert payload["database"]["path"] == str(target)
    assert payload["logging"]["path"] == str(root / "logs" / "research-gateway.log")
    assert payload["backup"]["directory"] == str(root / "backups")
    assert payload["runtime"]["directory"] == str(root / "runtime")
    assert payload["scopus"]["api_key"] == "test-secret"
    assert old_database.exists()
    assert result.config_backup.is_file()
    assert result.database_path == target


def test_relocate_storage_is_safe_to_repeat(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[database]\npath = "unused.db"\n', encoding="utf-8")
    root = tmp_path / "research-gateway"

    first = relocate_storage(config, root)
    with sqlite3.connect(first.database_path) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.execute("INSERT INTO marker VALUES('kept')")

    second = relocate_storage(config, root)

    with sqlite3.connect(second.database_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("kept",)

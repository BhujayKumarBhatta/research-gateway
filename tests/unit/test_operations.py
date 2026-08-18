from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from research_gateway.config import Settings
from research_gateway.db.database import EvidenceDatabase
from research_gateway.operations.backups import ExcelBackupService
from research_gateway.operations.logging import configure_logging
from research_gateway.operations.service import ServiceManager


@pytest.mark.asyncio
async def test_excel_backup_creates_timestamped_and_latest_workbooks(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "data" / "research_gateway.db")
    await database.migrate()
    service = ExcelBackupService(database, tmp_path / "backups", retention_count=2)

    result = await service.create()

    assert result.path.is_file()
    assert result.latest_path.is_file()
    assert result.path != result.latest_path
    assert "Evidence" in load_workbook(result.latest_path, read_only=True).sheetnames


def test_file_logging_redacts_all_configured_secrets(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "database": {"path": tmp_path / "data" / "research_gateway.db"},
            "logging": {"path": tmp_path / "logs" / "research-gateway.log"},
            "scopus": {"api_key": "fixture-secret-value"},
        }
    )
    log_path = configure_logging(settings)
    logging.getLogger("research_gateway.test").warning(
        "credential=%s", settings.scopus.api_key.get_secret_value()
    )
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert log_path.is_file()
    assert "fixture-secret-value" not in log_path.read_text(encoding="utf-8")


def _service_manager(tmp_path: Path) -> ServiceManager:
    settings = Settings.model_validate(
        {
            "database": {"path": tmp_path / "data" / "research_gateway.db"},
            "logging": {"path": tmp_path / "logs" / "research-gateway.log"},
            "runtime": {"directory": tmp_path / "runtime"},
            "service": {"port": 8877},
        }
    )
    return ServiceManager(settings, tmp_path / "config.toml")


def test_service_start_records_validated_process_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _service_manager(tmp_path)
    statuses = iter([{"running": False}, {"running": True, "pid": 4321}])
    monkeypatch.setattr(manager, "status", lambda: next(statuses))
    waited: list[int] = []
    monkeypatch.setattr(manager, "_wait_for_health", waited.append)
    launched: list[tuple[list[str], dict[str, object]]] = []

    def popen(command: list[str], **kwargs: object) -> SimpleNamespace:
        launched.append((command, kwargs))
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr("research_gateway.operations.service.subprocess.Popen", popen)

    result = manager.start(tunnel=True)

    assert result == {"running": True, "pid": 4321, "started": True}
    assert waited == [4321]
    assert launched[0][0][-1] == "--tunnel"
    assert manager._read_state()["pid"] == 4321


def test_service_start_reuses_a_running_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _service_manager(tmp_path)
    monkeypatch.setattr(manager, "status", lambda: {"running": True, "pid": 99})
    assert manager.start(tunnel=False) == {"running": True, "pid": 99, "started": False}


def test_service_start_failure_terminates_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _service_manager(tmp_path)
    monkeypatch.setattr(manager, "status", lambda: {"running": False})
    monkeypatch.setattr(
        "research_gateway.operations.service.subprocess.Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=4321),
    )
    monkeypatch.setattr(
        manager,
        "_wait_for_health",
        lambda pid: (_ for _ in ()).throw(RuntimeError("not healthy")),
    )
    monkeypatch.setattr(manager, "_process_matches", lambda pid: True)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "research_gateway.operations.service.os.kill",
        lambda pid, sent: signals.append((pid, sent)),
    )

    with pytest.raises(RuntimeError, match="not healthy"):
        manager.start(tunnel=False)

    assert signals and signals[0][0] == 4321
    assert not manager.state_path.exists()


def test_service_stop_and_restart_use_validated_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _service_manager(tmp_path)
    manager.runtime_directory.mkdir(parents=True)
    manager.state_path.write_text('{"pid": 4321}', encoding="utf-8")
    matches = iter([True, False, False])
    monkeypatch.setattr(manager, "_process_matches", lambda pid: next(matches))
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "research_gateway.operations.service.os.kill",
        lambda pid, sent: signals.append((pid, sent)),
    )

    assert manager.stop(timeout_seconds=0) == {"running": False, "stopped": True, "pid": 4321}
    assert signals[0][0] == 4321
    assert not manager.state_path.exists()

    monkeypatch.setattr(manager, "stop", lambda: {"stopped": True})
    monkeypatch.setattr(manager, "start", lambda *, tunnel: {"running": True, "tunnel": tunnel})
    assert manager.restart(tunnel=True) == {"running": True, "tunnel": True}


def test_service_status_reports_safe_local_and_public_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _service_manager(tmp_path)
    manager.runtime_directory.mkdir(parents=True)
    manager.state_path.write_text(
        '{"pid": 4321, "started_at": "2026-08-18T00:00:00Z"}', encoding="utf-8"
    )
    manager.tunnel_state_path.write_text(
        '{"public_url": "https://safe.ngrok.app"}', encoding="utf-8"
    )
    monkeypatch.setattr(manager, "_process_matches", lambda pid: True)
    monkeypatch.setattr(manager, "_health_ok", lambda: True)

    result = manager.status()

    assert result["running"] is True
    assert result["local_ui_url"] == "http://127.0.0.1:8877/ui"
    assert result["local_mcp_url"] == "http://127.0.0.1:8877/mcp"
    assert result["public_mcp_url"] == "https://safe.ngrok.app/mcp"
    assert result["database_path"].endswith("research_gateway.db")


def test_service_helpers_reject_stale_state_and_validate_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _service_manager(tmp_path)
    manager.runtime_directory.mkdir(parents=True)
    manager.state_path.write_text("not-json", encoding="utf-8")
    assert manager._read_state() == {}
    assert manager._process_matches(1) is False

    class Response(BytesIO):
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        "research_gateway.operations.service.urllib.request.urlopen",
        lambda url, timeout: Response(b"ok"),
    )
    assert manager._health_ok() is True

    manager.state_path.write_text('{"pid": 123}', encoding="utf-8")
    monkeypatch.setattr(manager, "_process_matches", lambda pid: False)
    assert manager.status()["running"] is False
    assert not manager.state_path.exists()

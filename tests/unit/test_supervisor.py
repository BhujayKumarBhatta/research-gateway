from __future__ import annotations

import subprocess
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_gateway.config import Settings
from research_gateway.operations.supervisor import (
    SupervisorError,
    SystemdUserSupervisor,
    _safe_int,
    _unit_argument,
)


def _supervisor(tmp_path: Path) -> SystemdUserSupervisor:
    working_directory = tmp_path / "durable-repository"
    executable = working_directory / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("fixture", encoding="utf-8")
    settings = Settings.model_validate(
        {
            "database": {"path": tmp_path / "data" / "research_gateway.db"},
            "logging": {"path": tmp_path / "logs" / "research-gateway.log"},
            "runtime": {"directory": tmp_path / "runtime"},
        }
    )
    return SystemdUserSupervisor(
        settings,
        tmp_path / "config.toml",
        unit_directory=tmp_path / "systemd",
        python_executable=executable,
        working_directory=working_directory,
    )


def test_install_writes_owned_restart_unit_and_enables_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor(tmp_path)
    calls: list[tuple[str, ...]] = []

    def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(supervisor, "_run", run)

    result = supervisor.install(tunnel=True)

    unit = supervisor.unit_path.read_text(encoding="utf-8")
    assert "Managed by Research Gateway" in unit
    assert f"# ConfigPath={supervisor.config_path}" in unit
    assert f"WorkingDirectory={supervisor.working_directory}" in unit
    assert " config-check --config " in unit
    assert " serve --config " in unit
    assert " --tunnel" in unit
    assert "Restart=always" in unit
    assert "RestartSec=5" in unit
    assert "StartLimitIntervalSec=0" in unit
    assert f"StandardOutput=append:{supervisor.supervisor_log_path}" in unit
    assert calls == [
        ("daemon-reload",),
        ("enable", supervisor.unit_name),
    ]
    assert result["installed"] is True
    assert result["tunnel"] is True
    assert supervisor.manages_config is True


def test_install_refuses_to_overwrite_an_unowned_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor(tmp_path)
    supervisor.unit_path.parent.mkdir(parents=True)
    supervisor.unit_path.write_text("[Service]\nExecStart=/another/program\n", encoding="utf-8")
    monkeypatch.setattr(
        supervisor,
        "_run",
        lambda *args, **kwargs: pytest.fail("systemd must not be called"),
    )

    with pytest.raises(SupervisorError, match="not managed by Research Gateway"):
        supervisor.install(tunnel=True)


def test_owned_unit_is_bound_to_one_external_config(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    supervisor.unit_path.parent.mkdir(parents=True)
    supervisor.unit_path.write_text(
        "# Managed by Research Gateway\n# ConfigPath=/another/config.toml\n",
        encoding="utf-8",
    )

    assert supervisor.owned is True
    assert supervisor.manages_config is False
    with pytest.raises(SupervisorError, match="different Research Gateway config"):
        supervisor.uninstall()


def test_configure_tunnel_reloads_only_when_mode_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        supervisor,
        "_run",
        lambda *args, check=True: (
            calls.append(args) or subprocess.CompletedProcess(args, 0, "", "")
        ),
    )
    supervisor.install(tunnel=True)
    calls.clear()

    assert supervisor.configure_tunnel(True) is False
    assert supervisor.configure_tunnel(False) is True
    assert " --no-tunnel\n" in supervisor.unit_path.read_text(encoding="utf-8")
    assert calls == [("daemon-reload",)]


def test_temporary_working_directory_is_rejected(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)

    with pytest.raises(SupervisorError, match="temporary directory"):
        supervisor.validate_durable_location()


def test_durable_location_validation_checks_all_required_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor(tmp_path)
    supervisor.config_path.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(
        "research_gateway.operations.supervisor.tempfile.gettempdir", lambda: "/var/tmp"
    )

    supervisor.validate_durable_location()
    supervisor.working_directory = tmp_path / "missing-repository"
    with pytest.raises(SupervisorError, match="working directory does not exist"):
        supervisor.validate_durable_location()
    supervisor.working_directory = tmp_path / "durable-repository"
    supervisor.python_executable = tmp_path / "missing-python"
    with pytest.raises(SupervisorError, match="Python executable does not exist"):
        supervisor.validate_durable_location()
    supervisor.python_executable = tmp_path / "durable-repository" / ".venv" / "bin" / "python"
    supervisor.config_path.unlink()
    with pytest.raises(SupervisorError, match="config does not exist"):
        supervisor.validate_durable_location()


def test_status_identifies_a_healthy_supervised_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor(tmp_path)
    supervisor.unit_path.parent.mkdir(parents=True)
    supervisor.unit_path.write_text("# Managed by Research Gateway\n", encoding="utf-8")
    monkeypatch.setattr(
        supervisor,
        "_show_properties",
        lambda: {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": "4321",
            "NRestarts": "2",
            "ExecMainStatus": "0",
        },
    )
    manager = SimpleNamespace(
        status=lambda: {
            "running": True,
            "classification": "unmanaged",
            "pid": 4321,
            "public_mcp_url": "https://example.ngrok.app/mcp",
        }
    )

    result = supervisor.status(manager)

    assert result["classification"] == "supervised"
    assert result["managed"] is True
    assert result["supervisor_enabled"] is True
    assert result["supervisor_restarts"] == 2
    assert result["pid"] == 4321


def test_start_and_restart_wait_for_health_and_return_supervised_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor(tmp_path)
    supervisor.unit_path.parent.mkdir(parents=True)
    supervisor.unit_path.write_text(
        f"# Managed by Research Gateway\n# ConfigPath={supervisor.config_path}\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []
    waits: list[float] = []
    monkeypatch.setattr(
        supervisor,
        "_run",
        lambda *args, check=True: (
            calls.append(args) or subprocess.CompletedProcess(args, 0, "", "")
        ),
    )
    monkeypatch.setattr(supervisor, "_wait_for_health", waits.append)
    monkeypatch.setattr(
        supervisor,
        "status",
        lambda manager: {
            "running": True,
            "classification": "supervised",
            "managed": True,
            "pid": 4321,
        },
    )
    manager = SimpleNamespace()

    started = supervisor.start(manager)
    restarted = supervisor.restart(manager)

    assert calls == [
        ("reset-failed", supervisor.unit_name),
        ("start", supervisor.unit_name),
        ("reset-failed", supervisor.unit_name),
        ("restart", supervisor.unit_name),
    ]
    assert waits == [60.0, 60.0]
    assert started["started"] is True
    assert restarted["restarted"] is True


def test_uninstall_and_stop_remove_only_the_matching_owned_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        supervisor,
        "_run",
        lambda *args, check=True: (
            calls.append(args) or subprocess.CompletedProcess(args, 0, "", "")
        ),
    )
    assert supervisor.uninstall() == {"installed": False, "removed": False}
    supervisor.install(tunnel=False)
    calls.clear()
    statuses = iter([{"running": True}, {"running": False}])
    monkeypatch.setattr(supervisor, "status", lambda manager: next(statuses))
    monkeypatch.setattr(supervisor, "_health_ok", lambda: False)

    stopped = supervisor.stop(SimpleNamespace(), timeout_seconds=0)

    assert stopped["stopped"] is True
    assert calls == [("stop", supervisor.unit_name)]
    calls.clear()
    result = supervisor.uninstall()
    assert result == {"installed": False, "removed": True}
    assert calls == [
        ("stop", supervisor.unit_name),
        ("disable", supervisor.unit_name),
        ("daemon-reload",),
    ]


@pytest.mark.parametrize(
    ("active_state", "classification"),
    (("activating", "supervisor_starting"), ("failed", "supervisor_failed")),
)
def test_status_reports_supervisor_transition_and_failure_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_state: str,
    classification: str,
) -> None:
    supervisor = _supervisor(tmp_path)
    supervisor.unit_path.parent.mkdir(parents=True)
    supervisor.unit_path.write_text(
        f"# Managed by Research Gateway\n# ConfigPath={supervisor.config_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        supervisor,
        "_show_properties",
        lambda: {
            "ActiveState": active_state,
            "SubState": "auto-restart",
            "MainPID": "not-an-integer",
            "ExecMainStatus": "2",
        },
    )

    result = supervisor.status(SimpleNamespace(status=lambda: {"running": False}))

    assert result["classification"] == classification
    assert result["supervisor_exit_status"] == 2
    assert result["pid"] is None


def test_systemd_property_and_command_helpers_are_controlled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor(tmp_path)
    run = supervisor._run
    monkeypatch.setattr(
        supervisor,
        "_run",
        lambda *args, check=False: subprocess.CompletedProcess(
            args, 0, "ActiveState=active\nMalformed\nMainPID=4321\n", ""
        ),
    )
    assert supervisor._show_properties() == {"ActiveState": "active", "MainPID": "4321"}
    monkeypatch.setattr(
        supervisor,
        "_run",
        lambda *args, check=False: subprocess.CompletedProcess(args, 1, "", ""),
    )
    assert supervisor._show_properties() == {}

    monkeypatch.setattr(supervisor, "_run", run)
    monkeypatch.setattr(
        "research_gateway.operations.supervisor.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "failed"),
    )
    with pytest.raises(SupervisorError, match="action 'start' failed"):
        supervisor._run("start")
    assert supervisor._run("show", check=False).returncode == 1


def test_health_wait_and_response_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor(tmp_path)
    health_ok = supervisor._health_ok
    monkeypatch.setattr(supervisor, "_health_ok", lambda: True)
    supervisor._wait_for_health(0.1)

    monkeypatch.setattr(supervisor, "_health_ok", lambda: False)
    with pytest.raises(SupervisorError, match="did not reach local health"):
        supervisor._wait_for_health(0)
    monkeypatch.setattr(supervisor, "_health_ok", health_ok)

    class Response(BytesIO):
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        "research_gateway.operations.supervisor.urllib.request.urlopen",
        lambda url, timeout: Response(b'{"status":"ok","service":"research-gateway"}'),
    )
    assert supervisor._health_ok() is True
    monkeypatch.setattr(
        "research_gateway.operations.supervisor.urllib.request.urlopen",
        lambda url, timeout: Response(b"not-json"),
    )
    assert supervisor._health_ok() is False


def test_safe_unit_value_helpers() -> None:
    assert _safe_int("12") == 12
    assert _safe_int("not-an-integer") == 0
    assert _unit_argument(Path("plain/path")) == "plain/path"
    assert _unit_argument(Path("path with spaces")) == '"path with spaces"'
    assert _unit_argument(Path("percent%path")) == "percent%%path"
    with pytest.raises(SupervisorError, match="unsupported newline"):
        _unit_argument(Path("unsafe\npath"))

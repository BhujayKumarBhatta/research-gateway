from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from research_gateway.config import Settings

MANAGED_MARKER = "# Managed by Research Gateway"
UNIT_NAME = "research-gateway.service"


class SupervisorError(RuntimeError):
    """A safe operator-facing systemd supervision failure."""


class SystemdUserSupervisor:
    """Install and operate a restartable Research Gateway systemd user service."""

    def __init__(
        self,
        settings: Settings,
        config_path: Path,
        *,
        unit_directory: Path | None = None,
        python_executable: Path | None = None,
        working_directory: Path | None = None,
    ) -> None:
        self.settings = settings
        self.config_path = config_path.expanduser().absolute()
        self.unit_directory = (
            (unit_directory or Path.home() / ".config" / "systemd" / "user").expanduser().absolute()
        )
        self.unit_name = UNIT_NAME
        self.unit_path = self.unit_directory / self.unit_name
        self.python_executable = (python_executable or Path(sys.executable)).expanduser().absolute()
        self.working_directory = (working_directory or Path.cwd()).expanduser().absolute()
        application_log = (
            (
                settings.logging.path
                or settings.database.path.parent / "logs" / "research-gateway.log"
            )
            .expanduser()
            .absolute()
        )
        self.supervisor_log_path = application_log.with_name("research-gateway-supervisor.log")

    @property
    def installed(self) -> bool:
        return self.unit_path.is_file()

    @property
    def owned(self) -> bool:
        return self.installed and self._is_owned_unit()

    @property
    def configured_config_path(self) -> Path | None:
        try:
            content = self.unit_path.read_text(encoding="utf-8")
        except OSError:
            return None
        prefix = "# ConfigPath="
        for line in content.splitlines():
            if line.startswith(prefix):
                return Path(line.removeprefix(prefix)).expanduser().absolute()
        return None

    @property
    def manages_config(self) -> bool:
        return self.owned and self.configured_config_path == self.config_path

    @property
    def configured_tunnel(self) -> bool | None:
        try:
            content = self.unit_path.read_text(encoding="utf-8")
        except OSError:
            return None
        if " --tunnel\n" in content:
            return True
        if " --no-tunnel\n" in content:
            return False
        return None

    def validate_durable_location(self) -> None:
        temporary_root = Path(tempfile.gettempdir()).resolve()
        working_directory = self.working_directory.resolve()
        if working_directory == temporary_root or temporary_root in working_directory.parents:
            raise SupervisorError(
                "Refusing to install Research Gateway supervision from a temporary directory. "
                "Run the command from the durable repository checkout."
            )
        if not self.working_directory.is_dir():
            raise SupervisorError(
                f"Supervisor working directory does not exist: {self.working_directory}"
            )
        if not self.python_executable.is_file():
            raise SupervisorError(
                f"Supervisor Python executable does not exist: {self.python_executable}"
            )
        if not self.config_path.is_file():
            raise SupervisorError(f"Research Gateway config does not exist: {self.config_path}")

    def install(self, *, tunnel: bool) -> dict[str, Any]:
        if self.installed and not self._is_owned_unit():
            raise SupervisorError(
                f"Existing unit is not managed by Research Gateway: {self.unit_path}"
            )
        if self.owned and not self.manages_config:
            raise SupervisorError(
                "The existing supervisor belongs to a different Research Gateway config. "
                "Use that config to uninstall it before installing this one."
            )
        self.unit_directory.mkdir(parents=True, exist_ok=True)
        self.supervisor_log_path.parent.mkdir(parents=True, exist_ok=True)
        content = self._render_unit(tunnel=tunnel)
        changed = not self.installed or self.unit_path.read_text(encoding="utf-8") != content
        if changed:
            temporary = self.unit_path.with_suffix(".service.tmp")
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, self.unit_path)
        self._run("daemon-reload")
        self._run("enable", self.unit_name)
        return {
            "installed": True,
            "changed": changed,
            "tunnel": tunnel,
            "unit_path": str(self.unit_path),
            "supervisor_log_path": str(self.supervisor_log_path),
        }

    def uninstall(self) -> dict[str, Any]:
        if not self.installed:
            return {"installed": False, "removed": False}
        if not self._is_owned_unit():
            raise SupervisorError(
                f"Existing unit is not managed by Research Gateway: {self.unit_path}"
            )
        if not self.manages_config:
            raise SupervisorError(
                "The existing supervisor belongs to a different Research Gateway config. "
                "Use that config to uninstall it."
            )
        self._run("stop", self.unit_name, check=False)
        self._run("disable", self.unit_name, check=False)
        self.unit_path.unlink()
        self._run("daemon-reload")
        return {"installed": False, "removed": True}

    def configure_tunnel(self, tunnel: bool) -> bool:
        """Update only the owned unit's tunnel flag and reload it when needed."""
        self._require_matching_unit()
        current = self.configured_tunnel
        if current is None:
            raise SupervisorError(
                f"Research Gateway could not read the tunnel mode from: {self.unit_path}"
            )
        if current == tunnel:
            return False
        content = self.unit_path.read_text(encoding="utf-8")
        old = " --tunnel\n" if current else " --no-tunnel\n"
        new = " --tunnel\n" if tunnel else " --no-tunnel\n"
        updated = content.replace(old, new, 1)
        if updated == content:
            raise SupervisorError(
                f"Research Gateway could not update the tunnel mode in: {self.unit_path}"
            )
        temporary = self.unit_path.with_suffix(".service.tmp")
        temporary.write_text(updated, encoding="utf-8")
        os.replace(temporary, self.unit_path)
        self._run("daemon-reload")
        return True

    def start(self, manager: Any) -> dict[str, Any]:
        self._require_matching_unit()
        self._run("reset-failed", self.unit_name, check=False)
        self._run("start", self.unit_name)
        self._wait_for_health(60.0)
        return {**self.status(manager), "started": True}

    def restart(self, manager: Any) -> dict[str, Any]:
        self._require_matching_unit()
        self._run("reset-failed", self.unit_name, check=False)
        self._run("restart", self.unit_name)
        self._wait_for_health(60.0)
        return {**self.status(manager), "restarted": True}

    def stop(self, manager: Any, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        self._require_matching_unit()
        previous = self.status(manager)
        self._run("stop", self.unit_name)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and self._health_ok():
            time.sleep(0.1)
        return {
            **self.status(manager),
            "stopped": bool(previous.get("running") and not self._health_ok()),
        }

    def status(self, manager: Any) -> dict[str, Any]:
        base = dict(manager.status())
        properties = self._show_properties() if self.installed else {}
        active_state = properties.get("ActiveState", "inactive")
        sub_state = properties.get("SubState", "dead")
        enabled = properties.get("UnitFileState") in {
            "enabled",
            "enabled-runtime",
            "linked",
            "linked-runtime",
        }
        main_pid = _safe_int(properties.get("MainPID"))
        restarts = _safe_int(properties.get("NRestarts"))
        if active_state == "active" and base.get("running"):
            base.update(
                {
                    "classification": "supervised",
                    "managed": True,
                    "pid": main_pid or base.get("pid"),
                }
            )
        elif active_state in {"active", "activating"}:
            base.update(
                {
                    "running": False,
                    "classification": "supervisor_starting",
                    "managed": True,
                    "pid": main_pid or None,
                }
            )
        elif active_state == "failed":
            base.update(
                {
                    "running": False,
                    "classification": "supervisor_failed",
                    "managed": True,
                    "pid": None,
                }
            )
        base.update(
            {
                "supervisor_installed": self.installed,
                "supervisor_enabled": enabled,
                "supervisor_active_state": active_state,
                "supervisor_sub_state": sub_state,
                "supervisor_restarts": restarts,
                "supervisor_exit_status": _safe_int(properties.get("ExecMainStatus")),
                "supervisor_unit_path": str(self.unit_path),
                "supervisor_log_path": str(self.supervisor_log_path),
            }
        )
        return base

    def _render_unit(self, *, tunnel: bool) -> str:
        python = _unit_argument(self.python_executable)
        config = _unit_argument(self.config_path)
        working_directory = _unit_argument(self.working_directory)
        supervisor_log = _unit_argument(self.supervisor_log_path)
        tunnel_flag = "--tunnel" if tunnel else "--no-tunnel"
        return "\n".join(
            (
                MANAGED_MARKER,
                f"# ConfigPath={self.config_path}",
                "[Unit]",
                "Description=Research Gateway with automatic recovery",
                "Wants=network-online.target",
                "After=network-online.target",
                "StartLimitIntervalSec=0",
                "",
                "[Service]",
                "Type=simple",
                f"WorkingDirectory={working_directory}",
                "Environment=PYTHONUNBUFFERED=1",
                f"ExecStartPre={python} -m research_gateway.cli config-check --config {config}",
                f"ExecStart={python} -m research_gateway.cli serve --config {config} {tunnel_flag}",
                "Restart=always",
                "RestartSec=5",
                "TimeoutStartSec=90",
                "TimeoutStopSec=30",
                "KillMode=mixed",
                f"StandardOutput=append:{supervisor_log}",
                f"StandardError=append:{supervisor_log}",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            )
        )

    def _is_owned_unit(self) -> bool:
        try:
            return self.unit_path.read_text(encoding="utf-8").startswith(MANAGED_MARKER)
        except OSError:
            return False

    def _require_matching_unit(self) -> None:
        if not self.installed:
            raise SupervisorError(
                "Research Gateway supervision is not installed. Run `service install` first."
            )
        if not self._is_owned_unit():
            raise SupervisorError(
                f"Existing unit is not managed by Research Gateway: {self.unit_path}"
            )
        if not self.manages_config:
            raise SupervisorError(
                "The existing supervisor belongs to a different Research Gateway config."
            )

    def _show_properties(self) -> dict[str, str]:
        completed = self._run(
            "show",
            self.unit_name,
            "--no-pager",
            "--property=LoadState,UnitFileState,ActiveState,SubState,MainPID,NRestarts,ExecMainStatus",
            check=False,
        )
        if completed.returncode:
            return {}
        properties: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key] = value
        return properties

    def _wait_for_health(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._health_ok():
                return
            time.sleep(0.2)
        raise SupervisorError(
            "Research Gateway supervisor did not reach local health. "
            f"Check the logs: {self.supervisor_log_path}"
        )

    def _health_ok(self) -> bool:
        url = f"http://{self.settings.service.host}:{self.settings.service.port}/health"
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status != 200:
                    return False
                payload = json.loads(response.read(65_536))
                return bool(
                    isinstance(payload, dict)
                    and payload.get("status") == "ok"
                    and payload.get("service") == "research-gateway"
                )
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["systemctl", "--user", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check and completed.returncode:
            action = args[0] if args else "command"
            raise SupervisorError(
                f"systemd user-service action '{action}' failed. "
                f"Check the supervisor log: {self.supervisor_log_path}"
            )
        return completed


def _safe_int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _unit_argument(value: Path) -> str:
    text = str(value)
    if "\n" in text or "\r" in text:
        raise SupervisorError("A supervisor path contains an unsupported newline.")
    text = text.replace("%", "%%")
    if any(character.isspace() or character in {'"', "\\"} for character in text):
        text = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{text}"'
    return text

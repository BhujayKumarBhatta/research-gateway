from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_gateway.config import Settings


class ServiceStartError(RuntimeError):
    """Expected operator-facing startup failure that is safe to print."""


class ServiceManager:
    """Manage one detached local Research Gateway process with validated PID state."""

    def __init__(self, settings: Settings, config_path: Path) -> None:
        self.settings = settings
        self.config_path = config_path.expanduser().absolute()
        self.runtime_directory = (
            (settings.runtime.directory or settings.database.path.parent / "runtime")
            .expanduser()
            .absolute()
        )
        self.state_path = self.runtime_directory / "service.json"
        self.tunnel_state_path = self.runtime_directory / "tunnel.json"
        self.log_path = (
            (
                settings.logging.path
                or settings.database.path.parent / "logs" / "research-gateway.log"
            )
            .expanduser()
            .absolute()
        )

    def start(self, *, tunnel: bool) -> dict[str, Any]:
        current = self.status()
        if current["running"]:
            return {**current, "started": False}
        if current.get("classification") == "port_conflict":
            raise ServiceStartError(
                f"Port {self.settings.service.port} is already in use by another service. "
                "Research Gateway was not started."
            )
        self.runtime_directory.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.unlink(missing_ok=True)
        self.tunnel_state_path.unlink(missing_ok=True)
        log_offset = self.log_path.stat().st_size if self.log_path.is_file() else 0
        command = [
            sys.executable,
            "-m",
            "research_gateway.cli",
            "serve",
            "--config",
            str(self.config_path),
            "--tunnel" if tunnel else "--no-tunnel",
        ]
        with self.log_path.open("a", encoding="utf-8") as stream:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        self.state_path.write_text(
            json.dumps(
                {
                    "pid": process.pid,
                    "started_at": datetime.now(UTC).isoformat(),
                    "tunnel_requested": tunnel,
                    "config_path": str(self.config_path),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            self._wait_for_health(process.pid)
        except Exception as exc:
            if self._process_matches(process.pid):
                os.kill(process.pid, signal.SIGTERM)
            self.state_path.unlink(missing_ok=True)
            raise self._friendly_start_error(exc, log_offset) from None
        return {**self.status(), "started": True}

    def stop(self, *, timeout_seconds: float = 20.0) -> dict[str, Any]:
        state = self._read_state()
        pid = int(state.get("pid") or 0)
        state_config = str(state.get("config_path") or "")
        config_matches = not state_config or (
            Path(state_config).expanduser().absolute() == self.config_path
        )
        if not pid or not config_matches or not self._process_matches(pid):
            current = self.status()
            return {**current, "stopped": False}
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and self._process_matches(pid):
            time.sleep(0.1)
        if self._process_matches(pid):
            os.kill(pid, signal.SIGKILL)
        self._clear_managed_state()
        return {"running": False, "stopped": True, "pid": pid}

    def restart(self, *, tunnel: bool) -> dict[str, Any]:
        self.stop()
        return self.start(tunnel=tunnel)

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        state_pid = int(state.get("pid") or 0)
        state_config = str(state.get("config_path") or "")
        state_matches = bool(
            state_pid
            and self._process_matches(state_pid)
            and (not state_config or Path(state_config).expanduser().absolute() == self.config_path)
        )
        gateway_healthy = self._health_ok()
        port_in_use = gateway_healthy or self._port_in_use()
        discovered = None if state_matches else self._discover_process()

        if gateway_healthy and state_matches:
            classification = "managed"
            pid = state_pid
        elif gateway_healthy:
            classification = "unmanaged"
            pid = discovered[0] if discovered else None
        elif port_in_use:
            classification = "port_conflict"
            pid = None
        else:
            classification = "stopped"
            pid = None
        running = classification in {"managed", "unmanaged"}
        state_stale = bool(state and classification != "managed")
        tunnel = self._read_json(self.tunnel_state_path) if running else {}
        tunnel_pid = int(tunnel.get("pid") or 0)
        tunnel_known = bool(
            tunnel.get("public_url")
            and pid
            and (tunnel_pid == pid or (classification == "managed" and not tunnel_pid))
        )
        observed_config = discovered[1] if discovered else None
        return {
            "running": running,
            "classification": classification,
            "managed": classification == "managed",
            "pid": pid,
            "started_at": state.get("started_at") if classification == "managed" else None,
            "state_stale": state_stale,
            "observed_config_path": str(observed_config) if observed_config else None,
            "local_ui_url": (
                f"http://{self.settings.service.host}:{self.settings.service.port}/ui"
                if running
                else None
            ),
            "local_mcp_url": (
                f"http://{self.settings.service.host}:{self.settings.service.port}/mcp"
                if running
                else None
            ),
            "tunnel_state": "known" if tunnel_known else ("unknown" if running else "stopped"),
            "public_url": tunnel.get("public_url") if tunnel_known else None,
            "public_mcp_url": (
                f"{str(tunnel['public_url']).rstrip('/')}/mcp" if tunnel_known else None
            ),
            "log_path": str(self.log_path),
            "database_path": str(self.settings.database.path),
        }

    def _wait_for_health(self, pid: int) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not self._process_matches(pid):
                raise RuntimeError("Research Gateway process exited during startup.")
            if self._health_ok():
                return
            time.sleep(0.2)
        raise RuntimeError("Research Gateway did not become healthy within 30 seconds.")

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

    def _port_in_use(self) -> bool:
        try:
            with socket.create_connection(
                (self.settings.service.host, self.settings.service.port), timeout=1
            ):
                return True
        except OSError:
            return False

    def _discover_process(self) -> tuple[int, Path | None] | None:
        fallback = None
        for candidate in Path("/proc").iterdir():
            if not candidate.name.isdigit():
                continue
            pid = int(candidate.name)
            command = self._process_command(pid)
            if not self._is_gateway_command(command):
                continue
            config_path = None
            if "--config" in command:
                index = command.index("--config")
                if index + 1 < len(command):
                    config_path = Path(command[index + 1]).expanduser().absolute()
            discovered = (pid, config_path)
            if config_path == self.config_path:
                return discovered
            fallback = fallback or discovered
        return fallback

    def _process_matches(self, pid: int) -> bool:
        if pid <= 1:
            return False
        return self._is_gateway_command(self._process_command(pid))

    @staticmethod
    def _process_command(pid: int) -> list[str]:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return []
        return [part.decode(errors="replace") for part in raw.split(b"\0") if part]

    @staticmethod
    def _is_gateway_command(command: list[str]) -> bool:
        return "research_gateway.cli" in command and "serve" in command

    def _read_state(self) -> dict[str, Any]:
        return self._read_json(self.state_path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _friendly_start_error(self, exc: Exception, log_offset: int) -> ServiceStartError:
        recent = ""
        try:
            with self.log_path.open("rb") as stream:
                size = self.log_path.stat().st_size
                stream.seek(log_offset if size >= log_offset else 0)
                recent = stream.read(65_536).decode(errors="replace")
        except OSError:
            pass
        if "ERR_NGROK_334" in recent:
            return ServiceStartError(
                "The configured ngrok endpoint is already online. "
                "Research Gateway was not started, and the existing endpoint was left intact."
            )
        if "address already in use" in recent.lower():
            return ServiceStartError(
                f"Port {self.settings.service.port} is already in use by another service. "
                "Research Gateway was not started."
            )
        if "exited during startup" in str(exc).lower():
            return ServiceStartError(
                f"Research Gateway exited during startup. Check the log: {self.log_path}"
            )
        return ServiceStartError(
            f"Research Gateway could not start. Check the log: {self.log_path}"
        )

    def _clear_managed_state(self) -> None:
        self.state_path.unlink(missing_ok=True)
        self.tunnel_state_path.unlink(missing_ok=True)

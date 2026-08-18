from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_gateway.config import Settings


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
        self.runtime_directory.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.unlink(missing_ok=True)
        self.tunnel_state_path.unlink(missing_ok=True)
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
        except Exception:
            if self._process_matches(process.pid):
                os.kill(process.pid, signal.SIGTERM)
            self.state_path.unlink(missing_ok=True)
            raise
        return {**self.status(), "started": True}

    def stop(self, *, timeout_seconds: float = 20.0) -> dict[str, Any]:
        state = self._read_state()
        pid = int(state.get("pid") or 0)
        if not pid or not self._process_matches(pid):
            self._clear_stale_state()
            return {"running": False, "stopped": False}
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and self._process_matches(pid):
            time.sleep(0.1)
        if self._process_matches(pid):
            os.kill(pid, signal.SIGKILL)
        self._clear_stale_state()
        return {"running": False, "stopped": True, "pid": pid}

    def restart(self, *, tunnel: bool) -> dict[str, Any]:
        self.stop()
        return self.start(tunnel=tunnel)

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        pid = int(state.get("pid") or 0)
        running = bool(pid and self._process_matches(pid) and self._health_ok())
        if not running and state:
            self._clear_stale_state()
            state = {}
        tunnel = self._read_json(self.tunnel_state_path) if running else {}
        return {
            "running": running,
            "pid": pid if running else None,
            "started_at": state.get("started_at"),
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
            "public_url": tunnel.get("public_url"),
            "public_mcp_url": (
                f"{str(tunnel['public_url']).rstrip('/')}/mcp" if tunnel.get("public_url") else None
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
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def _process_matches(self, pid: int) -> bool:
        if pid <= 1:
            return False
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            return False
        return "research_gateway.cli" in command and " serve " in f" {command} "

    def _read_state(self) -> dict[str, Any]:
        return self._read_json(self.state_path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _clear_stale_state(self) -> None:
        self.state_path.unlink(missing_ok=True)
        self.tunnel_state_path.unlink(missing_ok=True)

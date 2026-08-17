from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isawaitable
from pathlib import Path
from typing import Any, Protocol

import ngrok

from research_gateway.config import Settings


class NgrokBackend(Protocol):
    def forward(self, addr: int | str | None = None, **options: object) -> Any: ...
    def disconnect(self, url: str | None = None) -> Any: ...


@dataclass
class TunnelStatus:
    running: bool
    public_url: str | None
    public_health_url: str | None
    public_mcp_url: str | None
    exposed_paths: list[str]
    ui_exposed: bool


class NgrokTunnel:
    """Own one ngrok listener and report only the intentionally public paths."""

    def __init__(
        self,
        settings: Settings,
        *,
        backend: NgrokBackend = ngrok,
        state_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.backend = backend
        self.listener: Any | None = None
        self.public_url: str | None = None
        self.state_path = state_path or (
            Path.home() / ".research-gateway" / "runtime" / "tunnel.json"
        )

    def start(self) -> TunnelStatus:
        if self.listener is not None:
            return self.status()
        options = self._start_options()
        listener = self.backend.forward(self.settings.service.port, **options)
        if isawaitable(listener):
            if hasattr(listener, "cancel"):
                listener.cancel()
            raise RuntimeError("Use astart() when opening an ngrok listener inside an event loop.")
        return self._record_listener(listener)

    async def astart(self) -> TunnelStatus:
        """Open the listener without blocking an active application event loop."""
        if self.listener is not None:
            return self.status()
        options = self._start_options()
        listener = self.backend.forward(self.settings.service.port, **options)
        if isawaitable(listener):
            listener = await listener
        return self._record_listener(listener)

    def _start_options(self) -> dict[str, object]:
        if not self.settings.tunnel.enabled:
            raise RuntimeError("The ngrok tunnel is disabled in configuration.")
        if not self.settings.tunnel.configured:
            raise RuntimeError("The ngrok authtoken is not configured.")
        if (
            not self.settings.mcp_remote_auth.configured
            and not self.settings.mcp_remote_auth.allow_unauthenticated
        ):
            raise RuntimeError(
                "Remote MCP bearer authentication must be configured before tunneling."
            )
        if self.settings.service.host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("The local service must bind to loopback before tunneling.")
        options: dict[str, object] = {
            "authtoken": self.settings.tunnel.authtoken.get_secret_value(),
        }
        if self.settings.tunnel.domain:
            options["domain"] = self.settings.tunnel.domain
        return options

    def _record_listener(self, listener: Any) -> TunnelStatus:
        self.listener = listener
        self.public_url = str(self.listener.url()).rstrip("/")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(
                {
                    "public_url": self.public_url,
                    "pid": os.getpid(),
                    "started_at": datetime.now(UTC).isoformat(),
                    "exposed_paths": self._exposed_paths(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return self.status()

    def stop(self) -> TunnelStatus:
        if self.public_url:
            disconnected = self.backend.disconnect(self.public_url)
            if isawaitable(disconnected):
                if hasattr(disconnected, "cancel"):
                    disconnected.cancel()
                raise RuntimeError(
                    "Use astop() when closing an ngrok listener inside an event loop."
                )
        return self._clear()

    async def astop(self) -> TunnelStatus:
        """Close the listener without blocking an active application event loop."""
        if self.public_url:
            disconnected = self.backend.disconnect(self.public_url)
            if isawaitable(disconnected):
                await disconnected
        return self._clear()

    def _clear(self) -> TunnelStatus:
        self.listener = None
        self.public_url = None
        self.state_path.unlink(missing_ok=True)
        return self.status()

    def status(self) -> TunnelStatus:
        base = self.public_url
        return TunnelStatus(
            running=base is not None,
            public_url=base,
            public_health_url=f"{base}/health" if base else None,
            public_mcp_url=f"{base}/mcp" if base else None,
            exposed_paths=self._exposed_paths(),
            ui_exposed=self.settings.tunnel.expose_ui,
        )

    def _exposed_paths(self) -> list[str]:
        paths = ["/health", "/mcp"]
        if self.settings.tunnel.expose_ui:
            paths.extend(["/ui", "/api/v1"])
        return paths

    def __enter__(self) -> NgrokTunnel:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

from __future__ import annotations

import pytest

from research_gateway.config import Settings
from research_gateway.tunnel import NgrokTunnel


class Listener:
    def url(self) -> str:
        return "https://example.ngrok.app"


class Backend:
    def __init__(self) -> None:
        self.forwarded: tuple[object, dict[str, object]] | None = None
        self.disconnected: str | None = None

    def forward(self, addr=None, **options):
        self.forwarded = (addr, options)
        return Listener()

    def disconnect(self, url=None):
        self.disconnected = url


def test_tunnel_exposes_only_health_and_authenticated_mcp(tmp_path) -> None:
    settings = Settings.model_validate(
        {
            "tunnel": {"authtoken": "ngrok-secret"},
            "mcp_remote_auth": {"token": "mcp-secret"},
        }
    )
    backend = Backend()
    state_path = tmp_path / "tunnel.json"
    tunnel = NgrokTunnel(settings, backend=backend, state_path=state_path)
    status = tunnel.start()
    assert status.public_mcp_url == "https://example.ngrok.app/mcp"
    assert status.public_health_url == "https://example.ngrok.app/health"
    assert status.exposed_paths == ["/health", "/mcp"]
    assert status.ui_exposed is False
    assert backend.forwarded == (8765, {"authtoken": "ngrok-secret"})
    assert state_path.is_file()
    assert "ngrok-secret" not in state_path.read_text()
    tunnel.stop()
    assert backend.disconnected == "https://example.ngrok.app"
    assert not state_path.exists()


def test_tunnel_refuses_to_start_without_remote_auth(tmp_path) -> None:
    settings = Settings.model_validate({"tunnel": {"authtoken": "ngrok-secret"}})
    with pytest.raises(RuntimeError, match="bearer authentication"):
        NgrokTunnel(settings, backend=Backend(), state_path=tmp_path / "state.json").start()


@pytest.mark.asyncio
async def test_tunnel_supports_async_sdk_lifecycle(tmp_path) -> None:
    class AsyncBackend(Backend):
        async def forward(self, addr=None, **options):
            self.forwarded = (addr, options)
            return Listener()

        async def disconnect(self, url=None):
            self.disconnected = url

    settings = Settings.model_validate(
        {
            "tunnel": {"authtoken": "ngrok-secret"},
            "mcp_remote_auth": {"token": "mcp-secret"},
        }
    )
    backend = AsyncBackend()
    tunnel = NgrokTunnel(settings, backend=backend, state_path=tmp_path / "async.json")

    status = await tunnel.astart()
    assert status.public_mcp_url == "https://example.ngrok.app/mcp"
    stopped = await tunnel.astop()
    assert stopped.running is False
    assert backend.disconnected == "https://example.ngrok.app"

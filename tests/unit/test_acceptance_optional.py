from __future__ import annotations

import pytest

from research_gateway.acceptance import run_live_ieee, run_live_wos
from research_gateway.config import Settings


@pytest.mark.asyncio
async def test_pending_wos_and_ieee_live_gates_are_explicitly_deferred(capsys) -> None:
    settings = Settings.model_validate(
        {
            "wos": {"enabled": True, "mode": "starter", "api_key": "pending-key"},
            "ieee_xplore": {"enabled": True},
        }
    )
    await run_live_wos(settings)
    await run_live_ieee(settings)
    output = capsys.readouterr().out
    assert "WEB OF SCIENCE STARTER LIVE TEST DEFERRED — EXTERNAL APPROVAL PENDING" in output
    assert "WEB OF SCIENCE EXPANDED LIVE TEST DEFERRED — EXTERNAL APPROVAL PENDING" in output
    assert "IEEE XPLORE LIVE TEST DEFERRED — EXTERNAL APPROVAL PENDING" in output

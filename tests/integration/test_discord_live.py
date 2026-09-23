"""Live Discord checks. Skipped unless WAYPOINT_LIVE_TOKEN is set.

Use a *test* bot application, never the production token:

    set WAYPOINT_LIVE_TOKEN=...            (Windows)
    export WAYPOINT_LIVE_TOKEN=...         (macOS/Linux)
    python -m pytest tests/integration -m integration
"""

from __future__ import annotations

import os

import discord
import pytest

from bot.core import build_intents

TOKEN = os.getenv("WAYPOINT_LIVE_TOKEN", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TOKEN, reason="WAYPOINT_LIVE_TOKEN not set; live Discord tests skipped"),
]


async def test_token_logs_in_and_intents_are_enabled():
    """Proves the token is valid and the Server Members intent is switched on in the portal."""
    client = discord.Client(intents=build_intents())
    ready = {}

    @client.event
    async def on_ready() -> None:
        ready["user"] = client.user
        await client.close()

    await client.start(TOKEN)
    assert ready.get("user") is not None

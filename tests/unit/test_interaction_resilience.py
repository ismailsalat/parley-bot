from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import discord

from bot.services.panels import PanelService
from bot.views import base


class _Response:
    def __init__(self) -> None:
        self.done = False
        self.defer_calls: list[dict] = []

    def is_done(self) -> bool:
        return self.done

    async def defer(self, **kwargs) -> None:
        self.defer_calls.append(kwargs)
        self.done = True


class _Interaction:
    def __init__(self, *, interaction_type: discord.InteractionType) -> None:
        self.id = 123456
        self.type = interaction_type
        self.response = _Response()
        self.client = SimpleNamespace()
        self.user = SimpleNamespace(id=99)
        self.guild_id = 77


async def test_ack_watchdog_defers_slow_component(monkeypatch):
    monkeypatch.setattr(base, "ACK_WATCHDOG_SECONDS", 0.001)
    interaction = _Interaction(interaction_type=discord.InteractionType.component)

    base.arm_interaction_ack_watchdog(interaction)
    await asyncio.sleep(0.02)

    assert interaction.response.done is True
    assert interaction.response.defer_calls == [{"ephemeral": False, "thinking": False}]
    assert getattr(interaction.client, "_interaction_ack_watchdogs") == {}


async def test_ack_watchdog_does_nothing_after_normal_response(monkeypatch):
    monkeypatch.setattr(base, "ACK_WATCHDOG_SECONDS", 0.001)
    interaction = _Interaction(interaction_type=discord.InteractionType.component)

    base.arm_interaction_ack_watchdog(interaction)
    interaction.response.done = True
    await asyncio.sleep(0.02)

    assert interaction.response.defer_calls == []


async def test_intentional_panel_delete_is_not_recreated():
    service = PanelService(SimpleNamespace())
    service._intentional_panel_deletes[555] = time.monotonic() + 60

    # This must return before touching bot runtime/database state. The delete was
    # caused by Parley moving its own panel and a replacement is already in flight.
    await service.handle_message_deleted(channel_id=1, message_id=555)

    assert 555 not in service._intentional_panel_deletes

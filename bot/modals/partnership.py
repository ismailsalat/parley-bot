"""Modals for partnership requests and looking-for-partner posts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

from bot.views.base import arm_interaction_ack_watchdog, handle_error

TextSubmit = Callable[[discord.Interaction, str], Awaitable[None]]


class ShortMessageModal(discord.ui.Modal):
    """One optional short message, used for requests and looking-for-partner posts."""

    def __init__(self, *, title: str, label: str, placeholder: str, max_length: int, on_submit: TextSubmit) -> None:
        super().__init__(title=title[:45], timeout=600)
        self._callback = on_submit
        self.message = discord.ui.TextInput(
            label=label[:45],
            style=discord.TextStyle.paragraph,
            placeholder=placeholder[:100],
            required=False,
            max_length=max(1, max_length),
        )
        self.add_item(self.message)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        arm_interaction_ack_watchdog(interaction)
        await self._callback(interaction, self.message.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_error(interaction, error)

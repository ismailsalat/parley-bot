"""Modals for writing an advertisement and changing an invite link."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

from bot.config.runtime import DISCORD_MESSAGE_LIMIT
from bot.views.base import handle_error

AdSubmit = Callable[[discord.Interaction, str, str], Awaitable[None]]
InviteSubmit = Callable[[discord.Interaction, str], Awaitable[None]]

AD_PLACEHOLDER = "Paste your advertisement exactly as it should appear. Markdown, emojis and links are kept."
INVITE_PLACEHOLDER = "https://discord.gg/yourcode"


class AdvertisementModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        title: str,
        max_length: int,
        on_submit: AdSubmit,
        default_text: str | None = None,
        ask_invite: bool = False,
        invite_required: bool = False,
        default_invite: str | None = None,
    ) -> None:
        super().__init__(title=title[:45], timeout=900)
        self._callback = on_submit
        self.ad = discord.ui.TextInput(
            label="Advertisement",
            style=discord.TextStyle.paragraph,
            placeholder=AD_PLACEHOLDER,
            default=(default_text or "")[:max_length] or None,
            max_length=min(max_length, DISCORD_MESSAGE_LIMIT),
            required=True,
        )
        self.add_item(self.ad)
        self.invite: discord.ui.TextInput | None = None
        if ask_invite:
            label = "Invite link" if invite_required else "Invite link (optional)"
            self.invite = discord.ui.TextInput(
                label=label,
                placeholder=INVITE_PLACEHOLDER + " (leave blank and Parley will create one)",
                default=default_invite or None,
                required=False,
                max_length=200,
            )
            self.add_item(self.invite)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        invite = self.invite.value if self.invite is not None else ""
        await self._callback(interaction, self.ad.value, invite)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_error(interaction, error)


class InviteModal(discord.ui.Modal):
    def __init__(self, *, on_submit: InviteSubmit, default_invite: str | None = None) -> None:
        super().__init__(title="Server invite link", timeout=600)
        self._callback = on_submit
        self.invite = discord.ui.TextInput(
            label="Invite link",
            placeholder=INVITE_PLACEHOLDER + " (blank = let Parley create one)",
            default=default_invite or None,
            required=False,
            max_length=200,
        )
        self.add_item(self.invite)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._callback(interaction, self.invite.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_error(interaction, error)

"""Building blocks for admin screens.

Every admin screen is a ``Page``: an ephemeral (or DM) menu that edits itself in
place, has sensible Back / Home buttons and re-checks staff permissions on
every click.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

from bot.services import permissions
from bot.services.errors import ParleyError
from bot.utils.mentions import safe_allowed_mentions
from bot.views.base import OwnedView, handle_error, reply

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

PageFactory = Callable[[], "Page"]


class Page(OwnedView):
    """A settings screen. Subclasses implement ``content`` and ``build``."""

    title = "Settings"

    def __init__(self, bot: ParleyBot, owner_id: int, *, back: PageFactory | None = None) -> None:
        super().__init__(owner_id, timeout=900)
        self.bot = bot
        self.back = back
        self.notice: str | None = None

    # -- to implement
    def content(self) -> str:
        return f"## {self.title}"

    def embed(self) -> discord.Embed | None:
        return None

    def build(self) -> None:
        pass

    # -- helpers
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if not await permissions.is_staff_cached(self.bot, interaction.user.id):
            await reply(interaction, "Only Parley staff can use this.")
            return False
        return True

    def button(
        self,
        label: str,
        callback: Callable[[discord.Interaction], Awaitable[None]],
        *,
        emoji: str | None = None,
        style: discord.ButtonStyle = discord.ButtonStyle.primary,
        row: int | None = None,
        disabled: bool = False,
    ) -> discord.ui.Button:
        item = discord.ui.Button(label=label, emoji=emoji, style=style, row=row, disabled=disabled)

        async def run(interaction: discord.Interaction) -> None:
            try:
                await callback(interaction)
            except ParleyError as exc:
                await self.refresh(interaction, f"⚠️ {exc.user_message}")

        item.callback = run  # type: ignore[method-assign]
        self.add_item(item)
        return item

    def nav(self, row: int = 4) -> None:
        """Grey navigation, always last and on its own row."""
        if self.back is not None:
            self.button("Back", self._go_back, emoji="⬅️", style=discord.ButtonStyle.secondary, row=row)
        if not isinstance(self, _HomeMarker):
            self.button("Settings", self._go_home, emoji="🏠", style=discord.ButtonStyle.secondary, row=row)

    async def _go_back(self, interaction: discord.Interaction) -> None:
        assert self.back is not None
        self.stop()
        await self.back().show(interaction)

    async def _go_home(self, interaction: discord.Interaction) -> None:
        from bot.views.admin.settings import SettingsHome

        self.stop()
        await SettingsHome(self.bot, self.owner_id).show(interaction)

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        self.notice = notice
        self.clear_items()
        self.build()
        text = self.content()
        if self.notice:
            text = f"{self.notice}\n\n{text}"
        embed = self.embed()
        kwargs: dict[str, Any] = {
            "content": text[:2000],
            "view": self,
            "embeds": [embed] if embed else [],
            "allowed_mentions": safe_allowed_mentions(),
        }
        if interaction.response.is_done():
            await interaction.edit_original_response(**kwargs)
        elif interaction.message is not None and (interaction.guild is None or interaction.message.flags.ephemeral):
            await interaction.response.edit_message(**kwargs)
        else:
            kwargs.pop("embeds")
            if embed:
                kwargs["embed"] = embed
            await interaction.response.send_message(ephemeral=True, **kwargs)

    async def refresh(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        await self.show(interaction, notice)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await handle_error(interaction, error)


class _HomeMarker:
    """Mixin for the settings home (no Home button on itself)."""


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    default: str = ""
    placeholder: str = ""
    required: bool = False
    max_length: int = 100
    paragraph: bool = False


FieldsSubmit = Callable[[discord.Interaction, dict[str, str]], Awaitable[None]]


class FieldsModal(discord.ui.Modal):
    """Up to five text boxes; the callback receives {key: value}."""

    def __init__(self, title: str, fields: list[Field], on_submit: FieldsSubmit) -> None:
        super().__init__(title=title[:45], timeout=900)
        self._callback = on_submit
        self._inputs: dict[str, discord.ui.TextInput] = {}
        for spec in fields[:5]:
            text_input = discord.ui.TextInput(
                label=spec.label[:45],
                default=spec.default[: spec.max_length] or None,
                placeholder=spec.placeholder[:100] or None,
                required=spec.required,
                max_length=spec.max_length,
                style=discord.TextStyle.paragraph if spec.paragraph else discord.TextStyle.short,
            )
            self._inputs[spec.key] = text_input
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._callback(interaction, {key: item.value for key, item in self._inputs.items()})

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_error(interaction, error)


class ConfirmPage(Page):
    """Are you sure? [Confirm] [Cancel] — used for every dangerous action."""

    def __init__(
        self,
        bot: ParleyBot,
        owner_id: int,
        *,
        question: str,
        confirm_label: str,
        on_confirm: Callable[[discord.Interaction], Awaitable[None]],
        back: PageFactory,
        danger: bool = True,
    ) -> None:
        super().__init__(bot, owner_id, back=back)
        self.question = question
        self.confirm_label = confirm_label
        self.on_confirm = on_confirm
        self.danger = danger

    def content(self) -> str:
        return f"## Are you sure?\n{self.question}"

    def build(self) -> None:
        style = discord.ButtonStyle.danger if self.danger else discord.ButtonStyle.success
        self.button(self.confirm_label, self._confirm, style=style, row=0)
        self.button("Cancel", self._go_back, style=discord.ButtonStyle.secondary, row=1)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self.on_confirm(interaction)


def on_off(value: bool) -> str:
    return "On" if value else "Off"


def channel_mention(channel_id: int) -> str:
    return f"<#{channel_id}>" if channel_id else "not set"

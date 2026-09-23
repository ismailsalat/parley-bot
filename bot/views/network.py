"""Join Network: opt-in setup for receiving partner ads in a chosen channel."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from bot.database import repository
from bot.services import network, permissions
from bot.services.errors import MANAGE_SERVER_REQUIRED, PermissionDenied, ValidationError, ParleyError
from bot.utils.helpers import format_duration, utcnow
from bot.views.base import OwnedView, get_bot, home_button, reply
from bot.views.partnership import GuildPickerView
from bot.views.welcome import add_bot_button, persistent_view, register_action

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

ALL_CATEGORIES = "__all__"


def interval_label(minutes: int) -> str:
    from datetime import timedelta

    return "Every " + format_duration(timedelta(minutes=minutes)).removeprefix("1 ")


@register_action("network")
async def start_network_flow(interaction: discord.Interaction) -> None:
    bot = get_bot(interaction)
    guild = interaction.guild
    if guild is not None and guild.id != bot.runtime.hub.main_guild_id:
        if not isinstance(interaction.user, discord.Member) or not permissions.can_manage(interaction.user.guild_permissions):
            raise PermissionDenied(MANAGE_SERVER_REQUIRED)
        await open_network_setup(interaction, guild)
        return

    candidates = [g for g in permissions.cached_manageable_guilds(bot, interaction.user.id) if g.id != bot.runtime.hub.main_guild_id]
    if not candidates:
        add = add_bot_button(bot)
        await reply(
            interaction,
            "Add Parley to a server where you have **Manage Server**, then press **Join Network** "
            "(or use `/network`) inside it.",
            view=persistent_view(add) if add else None,
        )
        return
    if len(candidates) == 1:
        await open_network_setup(interaction, candidates[0])
        return

    async def picked(inter: discord.Interaction, guild_id: int) -> None:
        chosen = bot.get_guild(guild_id)
        if chosen is None:
            raise ValidationError("Parley is no longer in that server.")
        await open_network_setup(inter, chosen, edit_message=True)

    await reply(
        interaction,
        "Which server should receive network ads?",
        view=GuildPickerView(interaction.user.id, [(g.id, g.name) for g in candidates], picked),
    )


async def open_network_setup(interaction: discord.Interaction, guild: discord.Guild, *, edit_message: bool = False) -> None:
    bot = get_bot(interaction)
    await permissions.require_manager(bot, guild.id, interaction.user.id)
    async with bot.db.session() as session:
        current = await repository.get_network_settings(session, guild.id)
    view = NetworkSetupView(
        bot,
        interaction.user.id,
        guild,
        channel_id=current.channel_id if current else None,
        categories=list(current.categories or []) if current else [],
        interval=current.interval_minutes if current else bot.runtime.network.default_interval_minutes,
        enabled=bool(current and current.enabled),
        auto_partner=bool(current and current.auto_partner),
    )
    if edit_message:
        await interaction.response.edit_message(content=view.render(), view=view)
    else:
        await reply(interaction, view.render(), view=view)


class NetworkSetupView(OwnedView):
    def __init__(
        self,
        bot: ParleyBot,
        owner_id: int,
        guild: discord.Guild,
        *,
        channel_id: int | None,
        categories: list[str],
        interval: int,
        enabled: bool,
        auto_partner: bool,
    ) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.guild = guild
        self.channel_id = channel_id
        self.categories = [c for c in categories if c in bot.runtime.listings.categories]
        options = bot.runtime.network.interval_options
        self.interval = interval if interval in options else min(options, key=lambda o: abs(o - interval))
        self.enabled = enabled
        self.auto_partner = auto_partner
        self._build()

    def render(self, notice: str | None = None) -> str:
        channel = f"<#{self.channel_id}>" if self.channel_id else "choose below"
        lines = [
            f"## Parley Network · {self.guild.name}",
            "Confirmed partner ads and partnership requests go here. "
            "Parley never exchanges ads until both servers agree.",
            "",
            f"**Network:** {'🟢 Enabled' if self.enabled else '⚪ Off'}",
            f"**Channel:** {channel}",
            f"**Auto Partner:** {'🟢 On' if self.auto_partner else '⚪ Off'}",
            f"**Auto check:** {interval_label(self.interval)}",
        ]
        if self.bot.runtime.network.category_filtering:
            lines.append(f"**Categories:** {', '.join(self.categories) or 'All'}")
        if notice:
            lines.insert(0, f"{notice}\n")
        return "\n".join(lines)

    def _build(self) -> None:
        self.clear_items()
        config = self.bot.runtime
        channel = discord.ui.ChannelSelect(
            placeholder="Which channel should receive partnerships?",
            channel_types=[discord.ChannelType.text],
            default_values=[discord.Object(id=self.channel_id)] if self.channel_id else [],
            row=0,
        )
        channel.callback = self._on_channel  # type: ignore[method-assign]
        self._channel = channel
        self.add_item(channel)

        if config.network.category_filtering:
            names = list(config.listings.categories)
            categories = discord.ui.Select(
                placeholder="Categories (leave on All to accept every category)",
                min_values=1,
                max_values=len(names) + 1,
                options=[discord.SelectOption(label="All categories", value=ALL_CATEGORIES, default=not self.categories)]
                + [discord.SelectOption(label=c, value=c, default=c in self.categories) for c in names],
                row=1,
            )
            categories.callback = self._on_categories  # type: ignore[method-assign]
            self._categories = categories
            self.add_item(categories)

        interval = discord.ui.Select(
            placeholder="How often",
            options=[
                discord.SelectOption(label=interval_label(v), value=str(v), default=v == self.interval)
                for v in config.network.interval_options
            ],
            row=2,
        )
        interval.callback = self._on_interval  # type: ignore[method-assign]
        self._interval = interval
        self.add_item(interval)

        enable = discord.ui.Button(
            label="Save" if self.enabled else "Enable Network", emoji="🌐", style=discord.ButtonStyle.success, row=3
        )
        enable.callback = self._enable  # type: ignore[method-assign]
        self.add_item(enable)
        if self.enabled:
            auto = discord.ui.Button(
                label="Turn Auto Partner Off" if self.auto_partner else "Turn Auto Partner On",
                emoji="🤖",
                style=discord.ButtonStyle.secondary if self.auto_partner else discord.ButtonStyle.primary,
                row=3,
            )
            auto.callback = self._toggle_auto  # type: ignore[method-assign]
            self.add_item(auto)
            pause = discord.ui.Button(label="Pause Network", emoji="⏸️", style=discord.ButtonStyle.primary, row=3)
            pause.callback = self._disable  # type: ignore[method-assign]
            self.add_item(pause)
        if self.enabled or self.channel_id:
            leave = discord.ui.Button(label="Leave Network", style=discord.ButtonStyle.danger, row=3)
            leave.callback = self._leave  # type: ignore[method-assign]
            self.add_item(leave)
        self.add_item(home_button(self.bot, row=3))

    async def _rerender(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        self._build()
        await interaction.response.edit_message(content=self.render(notice), view=self)

    async def _on_channel(self, interaction: discord.Interaction) -> None:
        self.channel_id = self._channel.values[0].id
        await self._rerender(interaction)

    async def _on_categories(self, interaction: discord.Interaction) -> None:
        values = self._categories.values
        self.categories = [] if ALL_CATEGORIES in values else list(values)
        await self._rerender(interaction)

    async def _on_interval(self, interaction: discord.Interaction) -> None:
        self.interval = int(self._interval.values[0])
        await self._rerender(interaction)

    def _check_channel(self) -> None:
        if self.channel_id is None:
            raise ValidationError("Please choose a channel first.")
        channel = self.guild.get_channel(self.channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise ValidationError("Please choose a text channel in this server.")
        missing = permissions.missing_channel_permissions(channel, self.guild.me)
        if missing:
            raise ValidationError(f"Parley needs {', '.join(missing)} in {channel.mention}.")

    async def _save(self, interaction: discord.Interaction, enabled: bool) -> None:
        bot = self.bot
        try:
            await permissions.require_manager(bot, self.guild.id, interaction.user.id)
            if enabled:
                self._check_channel()
            async with bot.db.session() as session:
                await network.configure(
                    session,
                    bot.runtime,
                    guild_id=self.guild.id,
                    channel_id=self.channel_id,
                    categories=self.categories,
                    interval_minutes=self.interval,
                    enabled=enabled,
                    actor_id=interaction.user.id,
                    now=utcnow(),
                )
        except ParleyError as exc:
            await self._rerender(interaction, f"⚠️ {exc.user_message}")
            return
        self.enabled = enabled
        if not enabled:
            self.auto_partner = False
        if enabled:
            notice = "✅ Network enabled. Only confirmed partner ads and requests will be posted here."
            if bot.runtime.hub.mode != "live":
                notice += "\n-# 🧪 Parley is in TEST mode."
        elif self.channel_id is None:
            notice = "You left the network."
        else:
            notice = "⏸️ Network paused."
        await self._rerender(interaction, notice)
        await bot.log_event(
            f"🌐 **{self.guild.name}** (`{self.guild.id}`) {'enabled' if enabled else 'disabled'} network ads "
            f"({interaction.user.mention})."
        )

    async def _enable(self, interaction: discord.Interaction) -> None:
        await self._save(interaction, True)

    async def _disable(self, interaction: discord.Interaction) -> None:
        await self._save(interaction, False)

    async def _toggle_auto(self, interaction: discord.Interaction) -> None:
        try:
            await permissions.require_manager(self.bot, self.guild.id, interaction.user.id)
            self._check_channel()
            target = not self.auto_partner
            async with self.bot.db.session() as session:
                await network.set_auto_partner(
                    session, guild_id=self.guild.id, enabled=target,
                    actor_id=interaction.user.id, now=utcnow(),
                )
        except ParleyError as exc:
            await self._rerender(interaction, f"⚠️ {exc.user_message}")
            return
        self.auto_partner = target
        await self._rerender(
            interaction,
            "🤖 Auto Partner is on. Parley can find matches for you automatically."
            if target else "Auto Partner is off. You can still send requests manually.",
        )

    async def _leave(self, interaction: discord.Interaction) -> None:
        self.channel_id = None
        await self._save(interaction, False)

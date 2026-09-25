"""Join Network: opt-in setup for receiving partner ads in a chosen channel."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from bot.database import repository
from bot.database.models import ListingStatus
from bot.services import network, permissions
from bot.services.errors import MANAGE_SERVER_REQUIRED, PermissionDenied, ValidationError, ParleyError
from bot.utils.helpers import format_duration, utcnow
from bot.views.base import OwnedView, acknowledge, get_bot, home_button, reply
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

    candidates = [
        g for g in await permissions.manageable_guilds(bot, interaction.user.id)
        if g.id != bot.runtime.hub.main_guild_id
    ]
    if not candidates:
        add = add_bot_button(bot)
        embed = discord.Embed(
            title="⚠️ No Connected Server Found",
            description=(
                "The Parley Network works **inside a server where Parley is installed**.\n\n"
                "Press **Add Parley**, choose the server you manage, then check your **DMs from Parley** and finish setup. "
                "After that, come back here and choose the channel that should receive approved partner ads."
            ),
            color=bot.runtime.bot.color_warning,
        )
        await reply(interaction, embed=embed, view=persistent_view(add) if add else None)
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
        "Which connected server are you setting up the Network for?",
        view=GuildPickerView(interaction.user.id, [(g.id, g.name) for g in candidates], picked),
    )


async def open_network_setup(
    interaction: discord.Interaction,
    guild: discord.Guild,
    *,
    edit_message: bool = False,
    require_listing: bool = True,
    notice: str | None = None,
) -> None:
    """Open one server's Network setup.

    Partnership Network setup always has a live server ad behind it. If the ad
    is missing, the user is sent straight into the ad wizard and returned here
    automatically after the ad is created.
    """
    bot = get_bot(interaction)
    await permissions.require_manager(bot, guild.id, interaction.user.id)
    await bot.maybe_dm_manager_onboarding(guild, interaction.user)

    async with bot.db.session() as session:
        listing = await repository.get_listing(session, guild.id)
        current = await repository.get_network_settings(session, guild.id)

    if require_listing and (listing is None or listing.status != ListingStatus.ACTIVE):
        if listing is not None and listing.status == ListingStatus.PENDING:
            text = (
                f"## Partnership Setup · {guild.name}\n"
                "Your server ad is waiting for review. The Network can be enabled as soon as that ad is live.\n\n"
                "You do not need to create another ad."
            )
            view = persistent_view(home_button(bot))
            if edit_message or interaction.response.is_done():
                await interaction.edit_original_response(content=text, embeds=[], view=view)
            else:
                await reply(interaction, text, view=view)
            return

        from bot.views.listings import open_listing_form

        await open_listing_form(
            interaction, guild, edit_message=edit_message or interaction.response.is_done(), return_to_network=True
        )
        return

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
    content = view.render(notice)
    if edit_message or interaction.response.is_done():
        await interaction.edit_original_response(content=content, embeds=[], view=view)
    else:
        await reply(interaction, content, view=view)


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
        advanced: bool = False,
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
        self.advanced = advanced
        self._build()

    def render(self, notice: str | None = None) -> str:
        channel = f"<#{self.channel_id}>" if self.channel_id else "choose below"
        lines = [
            f"## 🤝 Partnership Setup · {self.guild.name}",
            "Your server ad is ready. Choose the **Partner Ad Channel** where approved partner ads should be delivered.",
            "",
            "**Server Ad:** ✅ Ready",
            f"**Partner Ad Channel:** {channel}",
            f"**Network:** {'🟢 Enabled' if self.enabled else '⚪ Off'}",
        ]
        if self.advanced:
            lines.extend(
                [
                    f"**Auto Partner:** {'🟢 On' if self.auto_partner else '⚪ Off'}",
                    f"**Auto check:** {interval_label(self.interval)}",
                ]
            )
            if self.bot.runtime.network.category_filtering:
                lines.append(f"**Auto Partner categories:** {', '.join(self.categories) or 'All'}")
            lines.append("-# Advanced settings only affect Auto Partner. Manual partnerships work without them.")
        else:
            lines.append("-# Nothing is exchanged until a partnership is accepted by both servers.")
        if notice:
            lines.insert(0, f"{notice}\n")
        return "\n".join(lines)

    def _build(self) -> None:
        self.clear_items()
        config = self.bot.runtime

        # Native ChannelSelect always reads channels from the guild where the
        # interaction happened. Network setup can be opened from the Parley hub,
        # so build a normal Select from the *target* guild instead.
        channels = list(self.guild.text_channels)
        channels.sort(key=lambda c: (c.position, c.id))
        if self.channel_id is not None:
            channels.sort(key=lambda c: 0 if c.id == self.channel_id else 1)
        channels = channels[:25]
        channel_options = [
            discord.SelectOption(
                label=(f"#{c.name}")[:100],
                value=str(c.id),
                description=(f"In {self.guild.name}")[:100],
                default=c.id == self.channel_id,
            )
            for c in channels
        ]
        channel = discord.ui.Select(
            placeholder=f"Partner Ad Channel in {self.guild.name}"[:150],
            options=channel_options or [discord.SelectOption(label="No text channels available", value="0")],
            disabled=not channel_options,
            row=0,
        )
        channel.callback = self._on_channel  # type: ignore[method-assign]
        self._channel = channel
        self.add_item(channel)

        if self.advanced and config.network.category_filtering:
            names = list(config.listings.categories)
            categories = discord.ui.Select(
                placeholder="Auto Partner categories",
                min_values=1,
                max_values=len(names) + 1,
                options=[discord.SelectOption(label="All categories", value=ALL_CATEGORIES, default=not self.categories)]
                + [discord.SelectOption(label=c, value=c, default=c in self.categories) for c in names],
                row=1,
            )
            categories.callback = self._on_categories  # type: ignore[method-assign]
            self._categories = categories
            self.add_item(categories)

        if self.advanced:
            interval = discord.ui.Select(
                placeholder="Auto Partner check interval",
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
            label="Save Network" if self.enabled else "Enable Network",
            emoji="🤝",
            style=discord.ButtonStyle.success,
            row=3,
        )
        enable.callback = self._enable  # type: ignore[method-assign]
        self.add_item(enable)

        if self.advanced and self.enabled:
            auto = discord.ui.Button(
                label="Auto Partner: On" if self.auto_partner else "Auto Partner: Off",
                emoji="🤖",
                style=discord.ButtonStyle.success if self.auto_partner else discord.ButtonStyle.secondary,
                row=3,
            )
            auto.callback = self._toggle_auto  # type: ignore[method-assign]
            self.add_item(auto)

        mode = discord.ui.Button(
            label="Simple View" if self.advanced else "Advanced",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        mode.callback = self._toggle_advanced  # type: ignore[method-assign]
        self.add_item(mode)

        if self.enabled or self.channel_id:
            leave = discord.ui.Button(label="Leave Network", style=discord.ButtonStyle.danger, row=3)
            leave.callback = self._leave  # type: ignore[method-assign]
            self.add_item(leave)
        self.add_item(home_button(self.bot, row=4))

    async def _rerender(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        self._build()
        if interaction.response.is_done():
            await interaction.edit_original_response(content=self.render(notice), view=self)
        else:
            await interaction.response.edit_message(content=self.render(notice), view=self)

    async def _toggle_advanced(self, interaction: discord.Interaction) -> None:
        self.advanced = not self.advanced
        await self._rerender(interaction)

    async def _on_channel(self, interaction: discord.Interaction) -> None:
        self.channel_id = int(self._channel.values[0])
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
        await acknowledge(interaction)
        bot = self.bot
        try:
            await permissions.require_manager(bot, self.guild.id, interaction.user.id)
            if enabled:
                self._check_channel()
            async with bot.db.session() as session:
                if enabled:
                    listing = await repository.get_listing(session, self.guild.id)
                    if listing is None or listing.status != ListingStatus.ACTIVE:
                        raise ValidationError("Your server ad must be live before the Partnership Network can be enabled.")
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
            notice = "✅ Network enabled. Accepted partnerships can now exchange ads through this channel."
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

    async def _toggle_auto(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
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

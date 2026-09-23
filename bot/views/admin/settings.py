"""/settings: the Parley control center (staff only)."""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import discord

from bot.services import configuration, health, permissions
from bot.services.setup import SLOTS
from bot.views.admin.common import ConfirmPage, Page, _HomeMarker, channel_mention
from bot.views.base import get_bot, home_button, reply
from bot.views.welcome import register_action

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

MODE_LABELS = {"test": "🧪 TEST", "live": "🟢 LIVE", "off": "🔴 OFF"}


@register_action("settings")
async def settings_button(interaction: discord.Interaction) -> None:
    await open_settings(interaction)


async def open_settings(interaction: discord.Interaction) -> None:
    bot = get_bot(interaction)
    await permissions.require_staff(bot, interaction.user.id)
    await SettingsHome(bot, interaction.user.id).show(interaction)


# ---------------------------------------------------------------- home


class SettingsHome(_HomeMarker, Page):
    title = "Parley Settings"

    def content(self) -> str:
        bot = self.bot
        hub = bot.runtime.hub
        guild = bot.get_guild(hub.main_guild_id) if hub.main_guild_id else None
        network = "Enabled" if bot.runtime.network.enabled else "Off"
        return "\n".join(
            [
                "## PARLEY SETTINGS",
                f"**Mode:** {MODE_LABELS[hub.mode]}",
                f"**Main Server:** {guild.name if guild else 'not set up — run /setup'}",
                f"**Listings:** {channel_mention(hub.listings_channel_id)}",
                f"**Network:** {network}",
                "-# Changes apply immediately. No restart needed.",
            ]
        )

    def build(self) -> None:
        from bot.views.admin import messages, moderation

        home = lambda: SettingsHome(self.bot, self.owner_id)  # noqa: E731
        o, b = self.owner_id, self.bot
        pages = [
            ("Server", "📍", lambda: ServerMenu(b, o, back=home)),
            ("Appearance", "🎨", lambda: messages.AppearancePage(b, o, back=home)),
            ("Rules", "📋", lambda: RulesMenu(b, o, back=home)),
            ("Moderation", "🚫", lambda: moderation.ModerationPage(b, o, back=home)),
            ("Tools", "🧰", lambda: ToolsMenu(b, o, back=home)),
        ]
        for label, emoji, factory in pages:
            self.button(label, _opener(factory), emoji=emoji, row=0)
        self.add_item(home_button(self.bot, row=1))  # back to the normal DM home

    async def _export(self, interaction: discord.Interaction) -> None:
        async with self.bot.db.session() as session:
            data = await configuration.export_settings(session, self.bot.runtime)
        file = discord.File(io.BytesIO(data.encode("utf-8")), filename="waypoint-settings.json")
        await reply(
            interaction,
            "📤 Your settings (no token, passwords or database details). "
            "To restore them, use **/admin import-settings** with this file.",
            file=file,
        )


class ServerMenu(Page):
    """Where Parley lives: its channels and who its staff are."""

    title = "Server"

    def content(self) -> str:
        hub = self.bot.runtime.hub
        guild = self.bot.get_guild(hub.main_guild_id) if hub.main_guild_id else None
        return f"## Server\n**{guild.name if guild else 'not set up'}** · listings in {channel_mention(hub.listings_channel_id)}"

    def build(self) -> None:
        back = lambda: ServerMenu(self.bot, self.owner_id, back=self.back)  # noqa: E731
        self.button("Channels", _opener(lambda: ChannelsPage(self.bot, self.owner_id, back=back)), emoji="📍", row=0)
        self.button("Staff", _opener(lambda: StaffPage(self.bot, self.owner_id, back=back)), emoji="🛡️", row=0)
        self.nav()


class ToolsMenu(Page):
    """Testing, health, mode and backups."""

    title = "Tools"

    def content(self) -> str:
        return f"## Tools\nMode: **{MODE_LABELS[self.bot.runtime.hub.mode]}**"

    def build(self) -> None:
        from bot.views.admin import test_center

        back = lambda: ToolsMenu(self.bot, self.owner_id, back=self.back)  # noqa: E731
        o, b = self.owner_id, self.bot
        self.button("Test", _opener(lambda: test_center.TestCenterPage(b, o, back=back)), emoji="🧪", row=0)
        self.button("Health Check", _opener(lambda: HealthPage(b, o, back=back)), emoji="🩺", row=0)
        self.button("Mode", _opener(lambda: ModePage(b, o, back=back)), emoji="🔀", row=0)
        self.button("Export Settings", self._export, emoji="📤", row=0)
        self.nav()

    async def _export(self, interaction: discord.Interaction) -> None:
        await SettingsHome._export(self, interaction)  # type: ignore[arg-type]


class RulesMenu(Page):
    """Listings, partnerships, network and categories, one at a time."""

    title = "Rules"

    def content(self) -> str:
        return "## Rules\nWhat do you want to change?"

    def build(self) -> None:
        from bot.views.admin import categories, rules

        back = lambda: RulesMenu(self.bot, self.owner_id, back=self.back)  # noqa: E731
        o, b = self.owner_id, self.bot
        self.button("Listings", _opener(lambda: rules.RulesPage(b, o, "listings", back=back)), emoji="📢", row=0)
        self.button("Partnerships", _opener(lambda: rules.RulesPage(b, o, "partnerships", back=back)), emoji="🤝", row=0)
        self.button("Network", _opener(lambda: rules.RulesPage(b, o, "network", back=back)), emoji="🌐", row=0)
        self.button("Categories", _opener(lambda: categories.CategoriesPage(b, o, back=back)), emoji="🏷️", row=0)
        self.nav()


class ModePage(Page):
    """Test, live or off."""

    title = "Mode"

    def content(self) -> str:
        mode = self.bot.runtime.hub.mode
        explain = {
            "test": "Only staff can use Parley. Network ads are paused.",
            "live": "Everyone can use Parley.",
            "off": "Users see a maintenance message. Staff still have Settings.",
        }[mode]
        return f"## Mode\n**{MODE_LABELS[mode]}** — {explain}"

    def build(self) -> None:
        current = self.bot.runtime.hub.mode
        back = lambda: ModePage(self.bot, self.owner_id, back=self.back)  # noqa: E731
        for value in ("test", "live", "off"):
            emoji, label = MODE_LABELS[value].split(" ", 1)
            self.button(
                label.title(), self._setter(value, back), emoji=emoji,
                style=discord.ButtonStyle.success if value == "live" else discord.ButtonStyle.primary,
                disabled=value == current, row=0,
            )
        self.nav(row=1)

    def _setter(self, mode: str, back):
        async def callback(interaction: discord.Interaction) -> None:
            from bot.views.admin.setup import GoLivePage, confirm_mode

            if mode == "live":
                await GoLivePage(self.bot, self.owner_id, back=back).show(interaction)
            else:
                await confirm_mode(self.bot, self.owner_id, mode, back=back).show(interaction)

        return callback


def _opener(factory):
    async def callback(interaction: discord.Interaction) -> None:
        await factory().show(interaction)

    return callback


# ---------------------------------------------------------------- channels


class ChannelsPage(Page):
    title = "Channels"

    def content(self) -> str:
        hub = self.bot.runtime.hub
        guild = self.bot.get_guild(hub.main_guild_id) if hub.main_guild_id else None
        lines = ["## Channels"]
        for slot in SLOTS:
            channel_id = getattr(hub, slot.key)
            channel = guild.get_channel(channel_id) if guild and channel_id else None
            if not channel_id:
                mark = "⚪" if not slot.required else "❌"
                lines.append(f"{mark} **{slot.label}:** not set")
            elif channel is None:
                lines.append(f"❌ **{slot.label}:** deleted — choose a new one")
            else:
                missing = [l for l, ok in permissions.channel_permission_report(channel, guild.me) if not ok]
                mark = "⚠️" if missing else "✅"
                extra = f" (missing {', '.join(missing)})" if missing else ""
                lines.append(f"{mark} **{slot.label}:** {channel.mention}{extra}")
        if self.bot.hub_env_fields:
            lines.append("\n⚠️ Some values come from the old `.env` file. Press **Save to Database** so you can remove them.")
        return "\n".join(lines)

    def build(self) -> None:
        self.button("Choose Channels", self._choose, emoji="⚙️", style=discord.ButtonStyle.primary, row=0)
        self.button("Repair Panels", self._repair, emoji="🔧", row=0)
        if self.bot.hub_env_fields:
            self.button("Save to Database", self._save_env, emoji="💾", row=0)
        self.button("Fix Permissions", self._fix_permissions, emoji="🛡️", row=1)
        self.nav()

    async def _choose(self, interaction: discord.Interaction) -> None:
        from bot.views.admin.setup import ChannelPicker

        guild = interaction.guild
        if guild is None or guild.id != (self.bot.runtime.hub.main_guild_id or guild.id):
            await self.refresh(interaction, "⚠️ Open **/settings** inside your main server to pick channels (Discord only lists a server's channels there).")
            return
        await ChannelPicker(self.bot, self.owner_id, guild, back=lambda: ChannelsPage(self.bot, self.owner_id, back=self.back)).show(interaction)

    async def _repair(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        results = await self.bot.panels.restore_panels(force_edit=True)
        words = {"ok": "fine", "created": "recreated", "moved": "moved to the bottom", "edited": "updated",
                 "disabled": "turned off", "no_channel": "no channel set", "error": "❌ failed (check permissions)"}
        summary = ", ".join(f"{name}: {words.get(state, state)}" for name, state in results.items())
        await self.show(interaction, f"🔧 Panels checked — {summary}.")

    async def _fix_permissions(self, interaction: discord.Interaction) -> None:
        await fix_permissions_page(self.bot, self.owner_id, back=lambda: ChannelsPage(self.bot, self.owner_id, back=self.back)).show(interaction)

    async def _save_env(self, interaction: discord.Interaction) -> None:
        changes = configuration.env_hub_changes(self.bot.runtime.hub, self.bot.hub_env_fields)
        async with self.bot.db.session() as session:
            await configuration.save(session, changes, actor_id=interaction.user.id)
        await self.bot.settings_changed()
        await self.show(interaction, "💾 Saved. You can now delete those lines from `.env` (only the token and database are needed).")


# ---------------------------------------------------------------- staff


class StaffPage(Page):
    title = "Staff"

    def _text(self) -> str:
        roles = self.bot.runtime.hub.staff_role_ids
        listed = ", ".join(f"<@&{r}>" for r in roles) or "none yet"
        return (
            "## Staff\n"
            f"**Staff roles:** {listed}\n"
            "Staff can use /settings, /admin, review listings and use Parley in TEST/OFF mode.\n"
            "-# Server administrators and the bot owner always have access."
        )

    in_guild = True

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        self.in_guild = interaction.guild is not None and interaction.guild.id == self.bot.runtime.hub.main_guild_id
        await super().show(interaction, notice)

    def content(self) -> str:  # type: ignore[override]
        text = StaffPage._text(self)
        if not self.in_guild:
            text += "\n\n⚠️ Open **/settings** in your main server to choose roles (Discord can't list roles in DMs)."
        return text

    def build(self) -> None:
        if self.in_guild:
            select = discord.ui.RoleSelect(
                placeholder="Choose staff roles",
                min_values=0,
                max_values=10,
                default_values=[discord.Object(id=r) for r in self.bot.runtime.hub.staff_role_ids[:10]],
                row=0,
            )
            select.callback = self._make_saver(select)  # type: ignore[method-assign]
            self.add_item(select)
        self.nav(row=1)

    def _make_saver(self, select: discord.ui.RoleSelect):
        async def callback(interaction: discord.Interaction) -> None:
            guild = interaction.guild
            if guild is None or guild.id != self.bot.runtime.hub.main_guild_id:
                await self.refresh(interaction, "⚠️ Choose staff roles from inside your main server.")
                return
            roles = [r.id for r in select.values if not r.is_default()]
            async with self.bot.db.session() as session:
                await configuration.save(session, {"hub.staff_role_ids": roles}, actor_id=interaction.user.id)
            await self.bot.settings_changed()
            await self.show(interaction, "✅ Staff roles saved.")

        return callback


# ---------------------------------------------------------------- health


class HealthPage(Page):
    title = "Health Check"

    def __init__(self, bot: ParleyBot, owner_id: int, *, back=None) -> None:
        super().__init__(bot, owner_id, back=back)
        self.checks: list[health.Check] = []
        self.details = False

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self.checks = await health.run(self.bot)
        await super().show(interaction, notice)

    def content(self) -> str:  # type: ignore[override]
        return health.render_details(self.checks) if self.details else health.render(self.checks)

    def build(self) -> None:
        fixes = {c.fix for c in self.checks if c.status != health.OK and c.fix}
        if "permissions" in fixes:
            self.button("Fix Permissions", self._fix, emoji="🛡️", row=0)
        if "repair" in fixes:
            self.button("Repair Panels", self._repair, emoji="🔧", row=0)
        if "channels" in fixes:
            self.button("Channels", self._channels, emoji="📍", row=0)
        self.button("Hide Details" if self.details else "Details", self._details, row=0)
        self.nav()

    async def _details(self, interaction: discord.Interaction) -> None:
        self.details = not self.details
        await self.show(interaction)

    async def _fix(self, interaction: discord.Interaction) -> None:
        await fix_permissions_page(self.bot, self.owner_id, back=lambda: HealthPage(self.bot, self.owner_id, back=self.back)).show(interaction)

    async def _repair(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.bot.panels.restore_panels(force_edit=True)
        await self.show(interaction, "🔧 Panels repaired.")

    async def _channels(self, interaction: discord.Interaction) -> None:
        await ChannelsPage(self.bot, self.owner_id, back=lambda: HealthPage(self.bot, self.owner_id, back=self.back)).show(interaction)

def reset_page(bot: ParleyBot, owner_id: int, section: str, label: str, *, back) -> Page:
    """Reset one section (with confirmation). Never resets everything at once."""

    async def apply(interaction: discord.Interaction) -> None:
        async with bot.db.session() as session:
            removed = await configuration.reset_section(session, section, actor_id=interaction.user.id)
        await bot.settings_changed(refresh_panels=section in ("messages", "appearance"))
        await back().show(interaction, f"↩️ {label} reset to defaults ({removed} change(s) removed).")

    return ConfirmPage(
        bot, owner_id,
        question=f"Reset **{label}** to the default values? Other sections are not affected.",
        confirm_label="Reset to Defaults", on_confirm=apply, back=back,
    )


def fix_permissions_page(bot: ParleyBot, owner_id: int, *, back) -> Page:
    """Repair only the channel overwrites Parley needs. Never asks for Administrator."""

    async def apply(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        guild = bot.get_guild(bot.runtime.hub.main_guild_id) if bot.runtime.hub.main_guild_id else None
        if guild is None:
            await back().show(interaction, "⚠️ Parley isn't in the main server.")
            return
        fixed, failed = await repair_channel_permissions(bot, guild)
        if failed:
            note = f"⚠️ Fixed {len(fixed)} channel(s). Parley needs **Manage Roles** (or channel permissions) for: {', '.join(failed)}."
        elif fixed:
            note = f"🛡️ Fixed permissions in {', '.join(fixed)}."
        else:
            note = "Nothing to fix: permissions are already correct."
        await back().show(interaction, note)

    return ConfirmPage(
        bot, owner_id,
        question="Give Parley the permissions it needs in its own channels? Nothing else is changed.",
        confirm_label="Fix Permissions", on_confirm=apply, back=back, danger=False,
    )


async def repair_channel_permissions(bot: ParleyBot, guild: discord.Guild) -> tuple[list[str], list[str]]:
    """Add Parley's own overwrite to each configured channel. Returns (fixed, failed)."""
    from bot.services.setup import bot_channel_permissions, listings_channel_permissions

    fixed, failed = [], []
    for slot in SLOTS:
        channel = guild.get_channel(getattr(bot.runtime.hub, slot.key) or 0)
        if not isinstance(channel, discord.TextChannel):
            continue
        if not any(not ok for _label, ok in permissions.channel_permission_report(channel, guild.me)):
            continue
        try:
            wanted = listings_channel_permissions() if slot.key == "listings_channel_id" else bot_channel_permissions()
            await channel.set_permissions(guild.me, overwrite=wanted, reason="Parley: fix permissions")
        except discord.HTTPException:
            failed.append(f"#{channel.name}")
        else:
            fixed.append(f"#{channel.name}")
    return fixed, failed

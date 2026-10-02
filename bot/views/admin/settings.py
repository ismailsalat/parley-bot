"""/settings: the Parley control center (staff only)."""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import discord

from bot.services import configuration, health, permissions
from bot.services.setup import SLOTS
from bot.views.admin.common import ConfirmPage, Field, FieldsModal, Page, _HomeMarker, channel_mention
from bot.views.base import acknowledge, get_bot, home_button, reply
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
                "## ⚙️ Parley Settings",
                f"**Bot:** {MODE_LABELS[hub.mode]} · **Posting:** {'Staff review' if bot.runtime.listings.approval_required else 'Automatic'}",
                f"**Server:** {guild.name if guild else 'not configured'} · **Network:** {network}",
                "Choose what you want to change below. Advanced settings are optional.",
            ]
        )

    def build(self) -> None:
        from bot.views.admin import moderation

        home = lambda: SettingsHome(self.bot, self.owner_id)  # noqa: E731
        o, b = self.owner_id, self.bot
        pages = [
            ("Posting", "📢", lambda: SimplePostingPage(b, o, back=home)),
            ("Review & Safety", "🛡️", lambda: moderation.ModerationPage(b, o, back=home)),
            ("Channels", "📍", lambda: ChannelsPage(b, o, back=home)),
            ("Fix & Test", "🔧", lambda: ToolsMenu(b, o, back=home)),
            ("Advanced", "⚙️", lambda: AdvancedSettingsPage(b, o, back=home)),
        ]
        for label, emoji, factory in pages:
            self.button(label, _opener(factory), emoji=emoji, row=0)
        self.add_item(home_button(self.bot, row=1))  # back to the normal DM home

    async def _export(self, interaction: discord.Interaction) -> None:
        async with self.bot.db.session() as session:
            data = await configuration.export_settings(session, self.bot.runtime)
        file = discord.File(io.BytesIO(data.encode("utf-8")), filename="parley-settings.json")
        await reply(
            interaction,
            "📤 Your settings (no token, passwords or database details). "
            "To restore them, use **/admin import-settings** with this file.",
            file=file,
        )


class SimplePostingPage(Page):
    """Small, usable front door for the posting settings most owners change."""

    title = "Posting"

    def content(self) -> str:
        rules = self.bot.runtime.listings
        mode = "Staff approval" if rules.approval_required else "Automatic (safety checks still apply)"
        return (
            f"## 📢 Posting\n**Mode:** {mode}\n"
            f"**Free repost:** {rules.quick_post_cooldown_minutes} minutes\n"
            f"**Connected repost:** {rules.connected_refresh_cooldown_minutes} minutes\n\n"
            "Change both timers here. Changes apply to existing listings too. 0 disables a timer; ownership and review rules still apply."
        )

    def build(self) -> None:
        approval = self.bot.runtime.listings.approval_required
        label = "Use Automatic Posting" if approval else "Require Staff Approval"
        self.button(label, self._toggle_approval, emoji="📝", row=0)
        self.button("Edit Repost Cooldowns", self._edit_cooldowns, row=0)
        self.button("Reset My Repost Timer", self._confirm_reset_timer, style=discord.ButtonStyle.danger, row=1)
        back = lambda: SimplePostingPage(self.bot, self.owner_id, back=self.back)  # noqa: E731
        from bot.views.admin import rules
        self.button("More Posting Options", _opener(lambda: rules.RulesPage(
            self.bot, self.owner_id, "listings", back=back,
        )), style=discord.ButtonStyle.secondary, row=1)
        self.nav(row=2)

    async def _edit_cooldowns(self, interaction: discord.Interaction) -> None:
        from bot.views.admin.rules import Number, parse_number
        specs = [
            Number("listings.quick_post_cooldown_minutes", "Free repost minutes (0 = off)", "minutes", 0, 10080),
            Number("listings.connected_refresh_cooldown_minutes", "Connected minutes (0 = off)", "minutes", 0, 10080),
        ]

        async def submitted(inter, values):
            await permissions.require_staff(self.bot, inter.user.id)
            changes = {spec.key: parse_number(spec, values[spec.key]) for spec in specs}
            await acknowledge(inter, thinking=False)
            async with self.bot.db.session() as session:
                await configuration.save(session, changes, actor_id=inter.user.id)
            await self.bot.settings_changed()
            await self.show(inter, "✅ Repost cooldowns saved and applied to existing listings.")

        await interaction.response.send_modal(FieldsModal("Repost Cooldowns", [
            Field(spec.key, spec.label, default=str(getattr(self.bot.runtime.listings, spec.key.split('.')[1])),
                  required=True, max_length=5) for spec in specs
        ], submitted))

    async def _confirm_reset_timer(self, interaction: discord.Interaction) -> None:
        from bot.services import testmode

        async def confirmed(inter):
            await acknowledge(inter, thinking=False)
            await testmode.reset_my_quick_cooldown(self.bot, actor_id=inter.user.id)
            await self.show(inter, "✅ Your Free repost timer is reset. Your ad and other users' timers are unchanged.")

        await ConfirmPage(
            self.bot, self.owner_id,
            question="Clear only your own Free repost timer, keeping your saved ad and its review status?",
            confirm_label="Reset My Timer", on_confirm=confirmed,
            back=lambda: SimplePostingPage(self.bot, self.owner_id, back=self.back),
        ).show(interaction)

    async def _toggle_approval(self, interaction: discord.Interaction) -> None:
        # Persist first; settings_changed reloads the effective runtime from DB.
        new_value = not self.bot.runtime.listings.approval_required
        await acknowledge(interaction, thinking=False)
        async with self.bot.db.session() as session:
            await configuration.save(
                session, {"listings.approval_required": new_value},
                actor_id=interaction.user.id,
            )
        await self.bot.settings_changed()
        await self.show(interaction, "✅ Posting mode updated.")


class AdvancedSettingsPage(Page):
    """Keep older settings reachable instead of removing supported commands."""

    title = "Advanced"

    def content(self) -> str:
        return "## Advanced Settings\nUse these options only when you need to customize Parley."

    def build(self) -> None:
        from bot.views.admin import messages

        back = lambda: AdvancedSettingsPage(self.bot, self.owner_id, back=self.back)  # noqa: E731
        bot, owner = self.bot, self.owner_id
        self.button("Server & Staff", _opener(lambda: ServerMenu(bot, owner, back=back)), row=0)
        self.button("Partnership & Network", _opener(lambda: RulesMenu(bot, owner, back=back)), row=0)
        self.button("Appearance & Text", _opener(lambda: messages.AppearancePage(bot, owner, back=back)), row=1)
        self.button("Other Tools", _opener(lambda: ToolsMenu(bot, owner, back=back)), row=1)
        self.nav(row=2)


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
    """One screen for safe repairs and the less common testing controls."""

    title = "Tools"

    def content(self) -> str:
        return ("## 🔧 Fix & Test\n"
                f"**Mode:** {MODE_LABELS[self.bot.runtime.hub.mode]}\n"
                "Repair buttons or refresh Discord commands without resetting any listings.")

    def build(self) -> None:
        from bot.views.admin import test_center

        back = lambda: ToolsMenu(self.bot, self.owner_id, back=self.back)  # noqa: E731
        o, b = self.owner_id, self.bot
        self.button("Health Check", _opener(lambda: HealthPage(b, o, back=back)), emoji="🩺", row=0)
        self.button("Repair Panels", self._repair_panels, emoji="🔧", row=0)
        self.button("Sync Commands", self._sync_commands, emoji="🔄", row=0)
        self.button("Test Center", _opener(lambda: test_center.TestCenterPage(b, o, back=back)), emoji="🧪", row=1)
        self.button("Mode", _opener(lambda: ModePage(b, o, back=back)), emoji="🔀", row=1)
        self.button("Export Settings", self._export, emoji="📤", row=1)
        self.nav()

    async def _repair_panels(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction, thinking=False)
        results = await self.bot.panels.refresh_entry_panels(repost=True)
        removed = await self.bot.panels.cleanup_orphan_entry_panels(history_limit=None)
        failures = [name for name, result in results.items() if result == "error"]
        await self.show(interaction,
                        (f"⚠️ Some panels still need attention: {', '.join(failures)}."
                         if failures else f"✅ Panels refreshed; {removed} old message(s) cleaned up."))

    async def _sync_commands(self, interaction: discord.Interaction) -> None:
        """Repair stale Discord commands without changing the DB or cooldowns."""
        await acknowledge(interaction, thinking=False)
        main_id = self.bot.runtime.hub.main_guild_id
        if not main_id:
            await self.show(interaction, "⚠️ Set up the Parley main server before syncing commands.")
            return
        await self.bot.register_staff_commands(main_id, sync=False)
        await self.bot._sync_commands()
        await self.show(interaction, "🔄 Command synchronization requested. Check Railway logs if any commands remain unavailable.")

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
        self.button("Parley Perks", self._perks_channel, emoji="💎", style=discord.ButtonStyle.primary, row=0)
        self.button("Repair Panels", self._repair, emoji="🔧", row=0)
        if self.bot.hub_env_fields:
            self.button("Save to Database", self._save_env, emoji="💾", row=1)
        self.button("Fix Permissions", self._fix_permissions, emoji="🛡️", row=1)
        self.nav()

    async def _choose(self, interaction: discord.Interaction) -> None:
        from bot.views.admin.setup import ChannelPicker

        guild = interaction.guild
        if guild is None or guild.id != (self.bot.runtime.hub.main_guild_id or guild.id):
            await self.refresh(interaction, "⚠️ Open **/settings** inside your main server to pick channels (Discord only lists a server's channels there).")
            return
        await ChannelPicker(self.bot, self.owner_id, guild, back=lambda: ChannelsPage(self.bot, self.owner_id, back=self.back)).show(interaction)

    async def _perks_channel(self, interaction: discord.Interaction) -> None:
        from bot.views.admin.setup import PerksPlacementPage

        guild = interaction.guild
        if guild is None or guild.id != (self.bot.runtime.hub.main_guild_id or guild.id):
            await self.refresh(interaction, "⚠️ Open **/settings** inside your main server to choose the Parley Perks channel.")
            return
        back = lambda: ChannelsPage(self.bot, self.owner_id, back=self.back)  # noqa: E731
        await PerksPlacementPage(
            self.bot, self.owner_id, guild, done=back, back=back, allow_skip=False
        ).show(interaction)

    async def _repair(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction, thinking=False)
        # Staff repair deliberately replaces the public entry panels so every
        # button is a fresh Discord component, not merely an edit of an old one.
        results = await self.bot.panels.refresh_entry_panels(repost=True)
        words = {"ok": "fine", "created": "recreated", "moved": "moved to the bottom", "edited": "updated",
                 "reposted": "reposted with fresh buttons", "disabled": "turned off", "no_channel": "no channel set", "error": "❌ failed (check permissions)"}
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
            await acknowledge(interaction, thinking=False)
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
        await acknowledge(interaction, thinking=False)
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
        await acknowledge(interaction, thinking=False)
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

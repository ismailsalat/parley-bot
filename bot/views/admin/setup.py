"""/setup: turn a server into the Parley hub with a few buttons.

Only the owner of the bot application can run it, so an unrelated server can't
claim itself as the central hub.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from bot.database import repository
from bot.services import configuration, permissions, testmode
from bot.services import setup as setup_service
from bot.services.errors import ValidationError
from bot.views.admin.common import ConfirmPage, Page, channel_mention
from bot.views.base import get_bot, reply
from bot.views.welcome import register_action, setup_invite_url

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- entry points


@register_action("setup")
async def setup_button(interaction: discord.Interaction) -> None:
    await start_setup(interaction)


async def start_setup(interaction: discord.Interaction) -> None:
    bot = get_bot(interaction)
    guild = interaction.guild
    if guild is None:
        await reply(interaction, "Run **/setup** inside the server that should become your Parley hub.")
        return
    await permissions.require_owner(bot, interaction.user.id)
    hub = bot.runtime.hub
    if hub.main_guild_id and hub.main_guild_id != guild.id:
        current = bot.get_guild(hub.main_guild_id)
        page = MoveHubPage(bot, interaction.user.id, guild, current.name if current else str(hub.main_guild_id))
    elif hub.setup_completed:
        page = AlreadySetUpPage(bot, interaction.user.id, guild)  # don't restart setup by accident
    else:
        page = SetupWelcome(bot, interaction.user.id, guild)
    await page.show(interaction)


class AlreadySetUpPage(Page):
    def __init__(self, bot: ParleyBot, owner_id: int, guild: discord.Guild) -> None:
        super().__init__(bot, owner_id)
        self.guild = guild

    def content(self) -> str:
        return f"## Parley is already set up\n**{self.guild.name}** is your main server."

    def build(self) -> None:
        self.button("Settings", self._settings, emoji="⚙️", row=0)
        self.button("Health Check", self._health, emoji="🩺", row=0)
        self.button("Reconfigure", self._reconfigure, row=0)

    async def _settings(self, interaction: discord.Interaction) -> None:
        from bot.views.admin.settings import SettingsHome

        await SettingsHome(self.bot, self.owner_id).show(interaction)

    async def _health(self, interaction: discord.Interaction) -> None:
        from bot.views.admin.settings import HealthPage

        await HealthPage(self.bot, self.owner_id, back=lambda: AlreadySetUpPage(self.bot, self.owner_id, self.guild)).show(interaction)

    async def _reconfigure(self, interaction: discord.Interaction) -> None:
        await SetupWelcome(self.bot, self.owner_id, self.guild).show(interaction)


# ---------------------------------------------------------------- saving


async def save_hub(bot: ParleyBot, guild: discord.Guild, channels: dict[str, int], *, actor_id: int) -> None:
    """Store the main server + channels. A first-time setup starts in TEST mode."""
    changes: dict[str, object] = {"hub.main_guild_id": guild.id, "hub.setup_completed": True}
    changes.update({f"hub.{key}": value for key, value in channels.items()})
    if not bot.runtime.hub.setup_completed:
        changes["hub.mode"] = "test"
    async with bot.db.session() as session:
        await configuration.save(session, changes, actor_id=actor_id)
        await repository.add_audit(session, "setup.saved", actor_id=actor_id, guild_id=guild.id)
    await bot.settings_changed()
    await bot.panels.restore_panels(force_edit=True)


def permission_report(bot: ParleyBot, guild: discord.Guild) -> tuple[str, bool]:
    """Checklist of Parley's permissions in each configured channel + how to fix problems."""
    hub = bot.runtime.hub
    lines: list[str] = []
    all_ok = True
    for slot in setup_service.SLOTS:
        channel = guild.get_channel(getattr(hub, slot.key) or 0)
        if not isinstance(channel, discord.TextChannel):
            if slot.required:
                all_ok = False
                lines.append(f"❌ **{slot.label}:** not chosen")
            else:
                lines.append(f"⚪ **{slot.label}:** not set (optional)")
            continue
        report = permissions.channel_permission_report(channel, guild.me)
        missing = [label for label, ok in report if not ok]
        marks = " ".join(f"{'✅' if ok else '❌'} {label}" for label, ok in report)
        lines.append(f"**{slot.label}** {channel.mention}\n{marks}")
        if missing:
            all_ok = False
    if guild.me.guild_permissions.create_instant_invite:
        lines.append("✅ Create Invite")
    else:
        lines.append("⚠️ Missing **Create Invite** (only needed for the Test Invite button)")
    if not all_ok:
        lines.append(
            "\n**How to fix:** open the channel's settings → **Permissions** → add **Parley** "
            "and allow the ❌ items. Then press **Check Again**."
        )
    return "\n".join(lines), all_ok


# ---------------------------------------------------------------- pages


class SetupWelcome(Page):
    title = "Set Up Parley"

    def __init__(self, bot: ParleyBot, owner_id: int, guild: discord.Guild) -> None:
        super().__init__(bot, owner_id)
        self.guild = guild

    def content(self) -> str:
        return (
            "## Set up Parley\n"
            "Automatic Setup creates the recommended channels.\n"
            "Choose Channels uses channels you already have."
        )

    def build(self) -> None:
        self.button("Automatic Setup", self._automatic, row=0)
        self.button("Choose Channels", self._manual, row=0)
        self.button("Cancel", self._cancel, style=discord.ButtonStyle.secondary, row=1)

    async def _automatic(self, interaction: discord.Interaction) -> None:
        if not setup_service.can_auto_setup(self.guild):
            await NoManageChannelsPage(self.bot, self.owner_id, self.guild).show(interaction)
            return
        await interaction.response.edit_message(content="⏳ Setting up your channels…", view=None, embeds=[])
        staff_roles = [r for r in (self.guild.get_role(rid) for rid in self.bot.runtime.hub.staff_role_ids) if r]
        result = await setup_service.automatic_setup(self.guild, staff_roles)
        await save_hub(
            self.bot, self.guild, {k: c.id for k, c in result.channels.items()}, actor_id=interaction.user.id
        )
        await ReadyPage(self.bot, self.owner_id, self.guild, result=result).show(interaction)

    async def _manual(self, interaction: discord.Interaction) -> None:
        await ChannelPicker(self.bot, self.owner_id, self.guild, back=lambda: SetupWelcome(self.bot, self.owner_id, self.guild)).show(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Setup cancelled. Run **/setup** any time.", view=None, embeds=[])


class MoveHubPage(Page):
    def __init__(self, bot: ParleyBot, owner_id: int, guild: discord.Guild, current_name: str) -> None:
        super().__init__(bot, owner_id)
        self.guild = guild
        self.current_name = current_name

    def content(self) -> str:
        return (
            "## Change the main server?\n"
            f"Parley's main server is currently **{self.current_name}**.\n"
            f"Make **{self.guild.name}** the main server instead? Listings will be posted here from now on."
        )

    def build(self) -> None:
        self.button("Move Main Server Here", self._move, style=discord.ButtonStyle.danger, row=0)
        self.button("Cancel", self._cancel, style=discord.ButtonStyle.secondary, row=1)

    async def _move(self, interaction: discord.Interaction) -> None:
        await SetupWelcome(self.bot, self.owner_id, self.guild).show(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Nothing was changed.", view=None, embeds=[])


class NoManageChannelsPage(Page):
    def __init__(self, bot: ParleyBot, owner_id: int, guild: discord.Guild) -> None:
        super().__init__(bot, owner_id, back=lambda: SetupWelcome(bot, owner_id, guild))
        self.guild = guild
        self.show_permission = False

    def content(self) -> str:
        text = "## I can't create channels automatically.\nChoose existing channels instead, or give me **Manage Channels**."
        if self.show_permission:
            text += (
                "\n\n**Manage Channels** lets Parley create its five channels. Parley never needs Administrator.\n"
                "Either give Parley's role **Manage Channels** in Server Settings → Roles, "
                "or re-add Parley with the button below (it asks for exactly the right permissions)."
            )
        return text

    def build(self) -> None:
        self.button("Choose Existing Channels", self._manual, row=0)
        if not self.show_permission:
            self.button("Show Required Permission", self._explain, row=0)
        else:
            url = setup_invite_url(self.bot)
            if url:
                self.add_item(discord.ui.Button(label="Re-add With Permission", emoji="➕", url=url, row=0))
            self.button("Try Again", self._retry, emoji="🔄", row=0)
        self.nav()

    async def _manual(self, interaction: discord.Interaction) -> None:
        await ChannelPicker(self.bot, self.owner_id, self.guild, back=lambda: SetupWelcome(self.bot, self.owner_id, self.guild)).show(interaction)

    async def _explain(self, interaction: discord.Interaction) -> None:
        self.show_permission = True
        await self.show(interaction)

    async def _retry(self, interaction: discord.Interaction) -> None:
        await SetupWelcome(self.bot, self.owner_id, self.guild).show(interaction)


class ChannelPicker(Page):
    """Pick existing channels with Discord's channel menus. Never asks for IDs."""

    title = "Choose Channels"

    def __init__(self, bot: ParleyBot, owner_id: int, guild: discord.Guild, *, back, page: int = 0) -> None:
        super().__init__(bot, owner_id, back=back)
        self.guild = guild
        self.page = page
        hub = bot.runtime.hub
        self.chosen: dict[str, int] = {
            slot.key: getattr(hub, slot.key) for slot in setup_service.SLOTS if hub.main_guild_id == guild.id
        }

    def slots(self):
        required = [s for s in setup_service.SLOTS if s.required]
        optional = [s for s in setup_service.SLOTS if not s.required]
        return required if self.page == 0 else optional

    def content(self) -> str:
        head = "## Choose Channels" + (" (optional)" if self.page else "")
        lines = [head]
        for slot in self.slots():
            lines.append(f"**{slot.label}:** {channel_mention(self.chosen.get(slot.key, 0))}")
        lines.append("\nPick below, then press **Save Setup**.")
        return "\n".join(lines)

    def build(self) -> None:
        for row, slot in enumerate(self.slots()):
            current = self.chosen.get(slot.key)
            select = discord.ui.ChannelSelect(
                placeholder=f"{slot.label} channel",
                channel_types=[discord.ChannelType.text],
                default_values=[discord.Object(id=current)] if current else [],
                min_values=0 if not slot.required else 1,
                row=row,
            )
            select.callback = self._make_setter(select, slot.key)  # type: ignore[method-assign]
            self.add_item(select)
        nav_row = len(self.slots())
        self.button("Save Setup", self._save, emoji="💾", row=nav_row)
        if self.page == 0:
            self.button("Optional Channels", self._next_page, emoji="➡️", row=nav_row)
        else:
            self.button("Required Channels", self._prev_page, emoji="⬅️", row=nav_row)
        if self.back is not None:
            self.button("Back", self._go_back, emoji="⬅️", style=discord.ButtonStyle.secondary, row=nav_row)

    def _make_setter(self, select: discord.ui.ChannelSelect, key: str):
        async def callback(interaction: discord.Interaction) -> None:
            self.chosen[key] = select.values[0].id if select.values else 0
            await self.show(interaction)

        return callback

    async def _next_page(self, interaction: discord.Interaction) -> None:
        self.page = 1
        await self.show(interaction)

    async def _prev_page(self, interaction: discord.Interaction) -> None:
        self.page = 0
        await self.show(interaction)

    async def _save(self, interaction: discord.Interaction) -> None:
        missing = [s.label for s in setup_service.SLOTS if s.required and not self.chosen.get(s.key)]
        if missing:
            raise ValidationError(f"Please choose: {', '.join(missing)}.")
        ids = [v for v in self.chosen.values() if v]
        if len(ids) != len(set(ids)):
            raise ValidationError("Please use a different channel for each purpose.")
        await interaction.response.defer()
        await save_hub(self.bot, self.guild, dict(self.chosen), actor_id=interaction.user.id)
        await ReadyPage(self.bot, self.owner_id, self.guild).show(interaction)


class ReadyPage(Page):
    def __init__(self, bot: ParleyBot, owner_id: int, guild: discord.Guild, *, result=None) -> None:
        super().__init__(bot, owner_id)
        self.guild = guild
        self.result = result

    def content(self) -> str:
        report, ok = permission_report(self.bot, self.guild)
        lines = ["## ✅ Parley is ready." if ok else "## ⚠️ Almost ready"]
        if self.result is not None:
            if self.result.created:
                lines.append("Created: " + ", ".join(f"#{n}" for n in self.result.created))
            if self.result.reused:
                lines.append("Reused existing: " + ", ".join(f"#{n}" for n in self.result.reused))
            if self.result.failed:
                lines.append("Couldn't create: " + ", ".join(f"#{n}" for n in self.result.failed))
        mode = self.bot.runtime.hub.mode
        if mode == "test":
            lines.append("\n🧪 Parley is in **TEST mode**: only staff can use it. Try everything, then press **Go Live**.")
        lines.append("\n" + report)
        return "\n".join(lines)

    def build(self) -> None:
        welcome = self.guild.get_channel(self.bot.runtime.hub.welcome_channel_id or 0)
        if isinstance(welcome, discord.TextChannel):
            self.add_item(discord.ui.Button(label="View Welcome", emoji="👋", url=welcome.jump_url, row=0))
        self.button("Open Settings", self._settings, emoji="⚙️", row=0)
        if self.bot.runtime.hub.mode != "live":
            self.button("Go Live", self._go_live, style=discord.ButtonStyle.success, row=0)
        self.button("Check Again", self._again, emoji="🔄", row=1)

    async def _settings(self, interaction: discord.Interaction) -> None:
        from bot.views.admin.settings import SettingsHome

        await SettingsHome(self.bot, self.owner_id).show(interaction)

    async def _go_live(self, interaction: discord.Interaction) -> None:
        await go_live_page(self.bot, self.owner_id, back=lambda: ReadyPage(self.bot, self.owner_id, self.guild)).show(interaction)

    async def _again(self, interaction: discord.Interaction) -> None:
        await self.show(interaction)


# ---------------------------------------------------------------- going live


def go_live_page(bot: ParleyBot, owner_id: int, *, back) -> Page:
    return GoLivePage(bot, owner_id, back=back)


class GoLivePage(Page):
    def __init__(self, bot: ParleyBot, owner_id: int, *, back) -> None:
        super().__init__(bot, owner_id, back=back)
        self.test_count: int | None = None

    def content(self) -> str:
        text = "## Go Live 🟢\nEveryone will be able to use Parley and network ads will start."
        if self.test_count:
            text += (
                f"\n\nThere are **{self.test_count} test listing(s)**. Delete them (recommended) "
                "or keep them as real listings?"
            )
        return text

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        async with self.bot.db.session() as session:
            self.test_count = len(await repository.test_listings(session))
        await super().show(interaction, notice)

    def build(self) -> None:
        if self.test_count:
            self.button("Delete Test Data & Go Live", self._live_delete, style=discord.ButtonStyle.success, row=0)
            self.button("Keep Them & Go Live", self._live_keep, style=discord.ButtonStyle.success, row=0)
        else:
            self.button("Go Live", self._live_delete, style=discord.ButtonStyle.success, row=0)
        self.button("Cancel", self._go_back, style=discord.ButtonStyle.secondary, row=1)

    async def _live(self, interaction: discord.Interaction, keep: bool) -> None:
        await interaction.response.defer()
        note = await testmode.go_live(self.bot, keep_listings=keep, actor_id=interaction.user.id)
        from bot.views.admin.settings import SettingsHome

        await SettingsHome(self.bot, self.owner_id).show(interaction, note)

    async def _live_delete(self, interaction: discord.Interaction) -> None:
        await self._live(interaction, keep=False)

    async def _live_keep(self, interaction: discord.Interaction) -> None:
        await self._live(interaction, keep=True)


def confirm_mode(bot: ParleyBot, owner_id: int, mode: str, *, back) -> Page:
    """TEST and OFF hide Parley from users, so they need a confirmation."""

    async def apply(interaction: discord.Interaction) -> None:
        async with bot.db.session() as session:
            await configuration.set_mode(session, mode, actor_id=interaction.user.id)
        await bot.settings_changed()
        await bot.log_event(f"⚙️ Mode set to **{mode.upper()}** by {interaction.user.mention}.")
        from bot.views.admin.settings import SettingsHome

        await SettingsHome(bot, owner_id).show(interaction, f"Mode is now **{mode.upper()}**.")

    question = {
        "test": "In **TEST** mode only staff can use Parley and network ads pause.",
        "off": "In **OFF** mode users see a maintenance message. Staff can still use Settings.",
    }[mode]
    return ConfirmPage(bot, owner_id, question=question, confirm_label=f"Switch to {mode.upper()}", on_confirm=apply, back=back)

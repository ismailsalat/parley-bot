"""Staff commands: /settings and /admin. Registered only in the main server.

They are attached to the main server live (after /setup), so staff never need
a restart. Everything here is also reachable through /settings buttons.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.database import repository
from bot.services import configuration, health, moderation, permissions
from bot.services.errors import ValidationError
from bot.utils.helpers import parse_snowflake, utcnow
from bot.views.admin.moderation import apply_staff_action, listing_embed
from bot.views.admin.settings import open_settings
from bot.views.base import acknowledge, reply
from bot.views.partnership import view_ad_button
from bot.views.welcome import persistent_view

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

REASON = "Shown in the audit log (optional)"
MAX_IMPORT_BYTES = 512_000


def parse_id(raw: str, what: str = "server") -> int:
    value = parse_snowflake(raw)
    if value is None:
        raise ValidationError(f"That doesn't look like a valid {what} ID.")
    return value


class StaffCommands(commands.Cog):
    admin = app_commands.Group(name="admin", description="Parley staff tools")

    def __init__(self, bot: ParleyBot) -> None:
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if not await permissions.is_staff(self.bot, interaction.user.id):
            await reply(interaction, "⚠️ Only Parley staff can use this.")
            return False
        return True

    async def _done(self, interaction: discord.Interaction, text: str) -> None:
        log.info("staff action by %s: %s", interaction.user.id, text)
        await self.bot.log_event(f"🛡️ {text} ({interaction.user.mention})")
        await reply(interaction, f"✅ {text}")

    @app_commands.command(name="settings", description="Parley settings (staff).")
    async def settings(self, interaction: discord.Interaction) -> None:
        await open_settings(interaction)

    @admin.command(name="remove", description="Remove a server's listing.")
    @app_commands.describe(guild_id="Server ID")
    async def remove(self, interaction: discord.Interaction, guild_id: str) -> None:
        gid = parse_id(guild_id)
        await apply_staff_action(self.bot, "remove", gid, interaction.user.id)
        await reply(interaction, f"✅ Removed the listing for `{gid}`.")

    @admin.command(name="suspend", description="Hide a listing temporarily (or restore it).")
    @app_commands.describe(guild_id="Server ID", restore="Choose True to lift a suspension")
    async def suspend(self, interaction: discord.Interaction, guild_id: str, restore: bool = False) -> None:
        gid = parse_id(guild_id)
        await acknowledge(interaction)
        await apply_staff_action(self.bot, "restore" if restore else "suspend", gid, interaction.user.id)
        await reply(interaction, f"✅ {'Restored' if restore else 'Suspended'} the listing for `{gid}`.")

    @admin.command(name="ban-server", description="Ban a server by ID from Parley.")
    @app_commands.describe(guild_id="Server ID")
    async def ban_server(self, interaction: discord.Interaction, guild_id: str) -> None:
        gid = parse_id(guild_id)
        await apply_staff_action(self.bot, "ban", gid, interaction.user.id)
        await reply(interaction, f"✅ Banned `{gid}`.")

    @admin.command(name="unban-server", description="Lift a server ban.")
    @app_commands.describe(guild_id="Server ID")
    async def unban_server(self, interaction: discord.Interaction, guild_id: str) -> None:
        gid = parse_id(guild_id)
        await apply_staff_action(self.bot, "unban", gid, interaction.user.id)
        await reply(interaction, f"✅ Unbanned `{gid}`. They can post a new listing.")

    @admin.command(name="block-user", description="Stop a user from using Parley.")
    @app_commands.describe(user="The user to block", reason=REASON)
    async def block_user(self, interaction: discord.Interaction, user: discord.User, reason: str | None = None) -> None:
        if user.id == interaction.user.id:
            raise ValidationError("You can't block yourself.")
        async with self.bot.db.session() as session:
            await moderation.block_user(session, user_id=user.id, reason=reason, moderator_id=interaction.user.id)
        self.bot.blocked_user_ids.add(user.id)
        await self._done(interaction, f"Blocked {user.mention} (`{user.id}`).")

    @admin.command(name="unblock-user", description="Allow a blocked user to use Parley again.")
    @app_commands.describe(user="The user to unblock")
    async def unblock_user(self, interaction: discord.Interaction, user: discord.User) -> None:
        async with self.bot.db.session() as session:
            await moderation.unblock_user(session, user_id=user.id, moderator_id=interaction.user.id)
        self.bot.blocked_user_ids.discard(user.id)
        await self._done(interaction, f"Unblocked {user.mention} (`{user.id}`).")

    @admin.command(name="listing", description="Inspect a server's listing.")
    @app_commands.describe(guild_id="Server ID")
    async def listing(self, interaction: discord.Interaction, guild_id: str) -> None:
        gid = parse_id(guild_id)
        embed, listing, _banned = await listing_embed(self.bot, gid)
        view = persistent_view(view_ad_button(self.bot, gid)) if listing is not None else None
        await reply(interaction, embed=embed, view=view)

    @admin.command(name="stats", description="Parley network statistics.")
    async def stats(self, interaction: discord.Interaction) -> None:
        async with self.bot.db.session() as session:
            numbers = await repository.stats(session, utcnow() - timedelta(hours=24))
        labels = {
            "guilds": "Known servers", "active_listings": "Active listings", "pending_listings": "Waiting for review",
            "suspended_listings": "Suspended listings", "pending_requests": "Pending requests",
            "accepted_partnerships": "Accepted partnerships", "network_destinations": "Network destinations",
            "network_posts_24h": "Network posts (24h)", "banned_guilds": "Banned servers", "blocked_users": "Blocked users",
        }
        lines = [f"**{labels.get(key, key)}:** {value:,}" for key, value in numbers.items()]
        lines.append(f"**Servers Parley is in:** {len(self.bot.guilds):,}")
        await reply(interaction, "## Parley stats\n" + "\n".join(lines))

    @admin.command(name="health", description="Run the Parley health check.")
    async def health_check(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        await reply(interaction, health.render(await health.run(self.bot)))

    @admin.command(name="repair-panels", description="Repost Parley panels with fresh buttons.")
    async def repair_panels(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction)
        results = await self.bot.panels.refresh_entry_panels(repost=True)
        refreshed, failed = await self.bot.panels.refresh_active_listing_views()
        summary = ", ".join(f"{name}={state}" for name, state in results.items())
        await reply(
            interaction,
            f"✅ Repaired public panels with fresh buttons.\n{summary}\n"
            f"Listing controls refreshed: {refreshed}; failed: {failed}.",
        )

    @admin.command(name="import-settings", description="Restore settings from an exported file.")
    @app_commands.describe(file="waypoint-settings.json from Export Settings", include_channels="Also restore main-server channels (same server only)")
    async def import_settings(self, interaction: discord.Interaction, file: discord.Attachment, include_channels: bool = False) -> None:
        if file.size > MAX_IMPORT_BYTES:
            raise ValidationError("That file is too big to be a Parley settings export.")
        raw = (await file.read()).decode("utf-8", errors="replace")
        chosen = configuration.parse_import(raw, include_hub=include_channels)
        async with self.bot.db.session() as session:
            count = await configuration.import_settings(session, chosen, actor_id=interaction.user.id)
        await self.bot.settings_changed(refresh_panels=True)
        await self._done(interaction, f"Imported {count} setting(s).")


STAFF_COMMAND_NAMES = ("settings", "admin")


async def setup(bot: ParleyBot) -> None:
    cog = StaffCommands(bot)
    await bot.add_cog(cog)
    # add_cog registers commands globally; staff commands live only in the main server.
    for command in cog.get_app_commands():
        bot.tree.remove_command(command.name)

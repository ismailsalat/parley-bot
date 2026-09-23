"""/manage: edit, Relist or remove your listings."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.database import repository
from bot.database.models import ListingStatus
from bot.services import permissions
from bot.views.management import show_management, show_my_servers

if TYPE_CHECKING:
    from bot.core import WaypointBot


class ManageCommands(commands.Cog):
    def __init__(self, bot: WaypointBot) -> None:
        self.bot = bot

    @app_commands.command(name="manage", description="Edit, Relist or remove your Waypoint listing.")
    async def manage(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is not None and isinstance(member, discord.Member) and permissions.can_manage(member.guild_permissions):
            async with self.bot.db.session() as session:
                listing = await repository.get_listing(session, guild.id)
            if listing is not None and listing.status != ListingStatus.REMOVED:
                await show_management(interaction, guild.id)
                return
        await show_my_servers(interaction)


async def setup(bot: WaypointBot) -> None:
    await bot.add_cog(ManageCommands(bot))

"""/manage: edit, Relist or remove your listings."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.database import repository
from bot.database.models import ListingStatus
from bot.services import permissions
from bot.services.errors import MANAGE_SERVER_REQUIRED, PermissionDenied
from bot.views.management import show_management, show_my_servers

if TYPE_CHECKING:
    from bot.core import ParleyBot


class ManageCommands(commands.Cog):
    def __init__(self, bot: ParleyBot) -> None:
        self.bot = bot

    @app_commands.command(name="manage", description="Edit, Relist or remove your Parley listing.")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
    @app_commands.default_permissions(manage_guild=True)
    async def manage(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is not None:
            if not isinstance(member, discord.Member) or not permissions.can_manage(member.guild_permissions):
                raise PermissionDenied(MANAGE_SERVER_REQUIRED)
            async with self.bot.db.session() as session:
                listing = await repository.get_listing(session, guild.id)
            if listing is not None and listing.status != ListingStatus.REMOVED:
                await show_management(interaction, guild.id)
                return
        await show_my_servers(interaction)

    @app_commands.command(name="relist", description="Move this server's Parley ad back to the top of the directory.")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def relist_command(self, interaction: discord.Interaction) -> None:
        from bot.views.management import relist

        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member) or not permissions.can_manage(member.guild_permissions):
            raise PermissionDenied(MANAGE_SERVER_REQUIRED)
        await relist(interaction, guild.id)


async def setup(bot: ParleyBot) -> None:
    await bot.add_cog(ManageCommands(bot))

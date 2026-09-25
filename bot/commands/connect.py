"""/connect: list the current server on Parley."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.services import permissions
from bot.services.errors import MANAGE_SERVER_REQUIRED, PermissionDenied
from bot.views.listings import open_listing_form

if TYPE_CHECKING:
    from bot.core import ParleyBot


class ConnectCommands(commands.Cog):
    def __init__(self, bot: ParleyBot) -> None:
        self.bot = bot

    @app_commands.command(name="connect", description="Post or manage this server's directory ad.")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def connect(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        # default_permissions only hides the command; always re-check live permissions.
        if not isinstance(member, discord.Member) or not permissions.can_manage(member.guild_permissions):
            raise PermissionDenied(MANAGE_SERVER_REQUIRED)
        assert interaction.guild is not None
        await open_listing_form(interaction, interaction.guild)


async def setup(bot: ParleyBot) -> None:
    await bot.add_cog(ConnectCommands(bot))

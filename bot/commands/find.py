"""/find: browse servers looking for partners."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.services import permissions
from bot.services.errors import MANAGE_SERVER_REQUIRED, PermissionDenied
from bot.views.partnership import start_find_flow

if TYPE_CHECKING:
    from bot.core import ParleyBot


class FindCommands(commands.Cog):
    def __init__(self, bot: ParleyBot) -> None:
        self.bot = bot

    @app_commands.command(name="find", description="Find servers to partner with.")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
    async def find(self, interaction: discord.Interaction) -> None:
        # In a connected customer server, the current server is the target and
        # the caller must manage it. In the Parley hub/DM this is a dashboard
        # command: the flow discovers the caller's authorized servers instead.
        guild = interaction.guild
        if guild is not None and guild.id != self.bot.runtime.hub.main_guild_id:
            member = interaction.user
            if not isinstance(member, discord.Member) or not permissions.can_manage(member.guild_permissions):
                raise PermissionDenied(MANAGE_SERVER_REQUIRED)
        await start_find_flow(interaction)


async def setup(bot: ParleyBot) -> None:
    await bot.add_cog(FindCommands(bot))

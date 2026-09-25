"""/network: opt this server in (or out) of receiving network ads."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.views.network import start_network_flow

if TYPE_CHECKING:
    from bot.core import ParleyBot


class NetworkCommands(commands.Cog):
    def __init__(self, bot: ParleyBot) -> None:
        self.bot = bot

    @app_commands.command(name="network", description="Set the channel used to exchange ads after approved partnerships.")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def network(self, interaction: discord.Interaction) -> None:
        await start_network_flow(interaction)


async def setup(bot: ParleyBot) -> None:
    await bot.add_cog(NetworkCommands(bot))

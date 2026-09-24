"""/find: browse servers looking for partners."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

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
        await start_find_flow(interaction)


async def setup(bot: ParleyBot) -> None:
    await bot.add_cog(FindCommands(bot))

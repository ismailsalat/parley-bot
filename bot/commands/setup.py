"""/setup: owner-only first-run wizard (works before anything is configured)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.views.admin.setup import start_setup

if TYPE_CHECKING:
    from bot.core import ParleyBot


class SetupCommands(commands.Cog):
    def __init__(self, bot: ParleyBot) -> None:
        self.bot = bot

    @app_commands.command(name="setup", description="Owner only: make this server your Parley hub.")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def setup_command(self, interaction: discord.Interaction) -> None:
        await start_setup(interaction)


async def setup(bot: ParleyBot) -> None:
    await bot.add_cog(SetupCommands(bot))

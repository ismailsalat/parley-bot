"""/help: a short explanation plus the control panel buttons."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import templates
from bot.views.base import reply
from bot.views.welcome import control_panel

if TYPE_CHECKING:
    from bot.core import WaypointBot


def help_text(bot: WaypointBot) -> str:
    text = templates.render(bot.runtime, "help")
    if bot.runtime.bot.support_url:
        text += "\n" + templates.render(bot.runtime, "support")
    return text


class HelpCommands(commands.Cog):
    def __init__(self, bot: WaypointBot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="How Waypoint works.")
    async def help(self, interaction: discord.Interaction) -> None:
        _content, view = control_panel(self.bot)

        await reply(interaction, help_text(self.bot), view=view)


async def setup(bot: WaypointBot) -> None:
    await bot.add_cog(HelpCommands(bot))

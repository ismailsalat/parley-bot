"""“Verify My Servers”: the botless route into the listing form.

Listing a server never requires installing Parley. The user proves which
servers they manage with a read-only Discord login, picks one, and continues
into the normal listing form. Adding Parley stays available next to it, clearly
optional, for the Connected perks.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from bot.services import verification
from bot.services.verification import VerifiedGuild
from bot.utils.helpers import utcnow
from bot.views.base import OwnedView, get_bot, home_button, reply
from bot.views.welcome import add_bot_button, invite_url

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

TITLE = "Post Your Server"
DESCRIPTION = (
    "Verify your Discord account to see the servers you manage.\n\n"
    "**You do not need to add Parley to list your server.**"
)
UNAVAILABLE = (
    "Listing verification isn't set up on this Parley instance yet. "
    "Ask a Parley admin to finish the **Verify My Servers** configuration."
)


async def start_verification(interaction: discord.Interaction, *, notice: str | None = None) -> None:
    """The screen shown when Parley has nothing installed to verify against."""
    bot = get_bot(interaction)
    if not bot.settings.oauth_enabled or not (bot.settings.oauth_client_id or bot.application_id):
        # Degrade honestly rather than pretending the button works.
        await reply(interaction, f"## {TITLE}\n{UNAVAILABLE}")
        return

    async with bot.db.session() as session:
        row = await verification.start(session, user_id=interaction.user.id, now=utcnow())
        state = row.state

    url = verification.authorize_url(
        client_id=int(bot.settings.oauth_client_id or bot.application_id),
        redirect_uri=bot.settings.oauth_redirect_uri,
        state=state,
    )
    view = VerifyView(bot, interaction.user.id, url)
    await reply(interaction, view.render(notice), view=view)


class VerifyView(OwnedView):
    """[Verify My Servers] [Continue] and an optional, clearly secondary [Add Parley]."""

    def __init__(self, bot: ParleyBot, owner_id: int, url: str) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.add_item(discord.ui.Button(label="Verify My Servers", emoji="🔐", url=url, row=0))
        cont = discord.ui.Button(label="Continue", style=discord.ButtonStyle.primary, row=0)
        cont.callback = self._continue  # type: ignore[method-assign]
        self.add_item(cont)
        # Optional: the perks, never the requirement.
        if invite_url(bot):
            add = add_bot_button(bot, row=1)
            if add is not None:
                self.add_item(add)

    def render(self, notice: str | None = None) -> str:
        lines = [f"## {TITLE}", DESCRIPTION, "", "Verify, then come back and press **Continue**."]
        if invite_url(self.bot):
            lines.append("-# Adding Parley is optional: it unlocks the Connected perks.")
        text = "\n".join(lines)
        return f"⚠️ {notice}\n\n{text}" if notice else text

    async def _continue(self, interaction: discord.Interaction) -> None:
        await show_verified_servers(interaction)


async def show_verified_servers(interaction: discord.Interaction) -> None:
    """After verification: the servers Discord says this account manages."""
    from bot.views.listings import open_verified_listing_form

    bot = get_bot(interaction)
    async with bot.db.session() as session:
        result = await verification.latest_verification(session, user_id=interaction.user.id, now=utcnow())
    guilds = result.guilds

    if not guilds:
        if result.completed:
            # Verification worked; Discord simply reported nothing they manage.
            await NoServersView(bot, interaction.user.id).show(interaction)
            return
        await start_verification(
            interaction,
            notice="Parley hasn't seen a completed verification yet. Press **Verify My Servers**, finish in the browser, then **Continue**.",
        )
        return

    if len(guilds) == 1:  # one obvious answer: don't ask the question
        await open_verified_listing_form(interaction, guilds[0])
        return

    from bot.views.partnership import GuildPickerView

    async def picked(inter: discord.Interaction, guild_id: int) -> None:
        chosen = next((g for g in guilds if g.id == guild_id), None)
        if chosen is None:  # never trust an id that didn't come from the verified set
            await start_verification(inter, notice=verification.STATE_UNKNOWN)
            return
        await open_verified_listing_form(inter, chosen)

    embed = discord.Embed(
        title="Select a Server",
        description="Choose a server you manage.",
        color=bot.runtime.bot.color_primary,
    )
    await reply(
        interaction,
        embed=embed,
        view=GuildPickerView(interaction.user.id, [(g.id, g.name) for g in guilds], picked),
    )


class NoServersView(OwnedView):
    """Verification finished, but this account manages nothing Parley can list."""

    def __init__(self, bot: ParleyBot, owner_id: int) -> None:
        super().__init__(owner_id)
        self.bot = bot
        again = discord.ui.Button(label="Verify Again", emoji="🔐", style=discord.ButtonStyle.primary, row=0)
        again.callback = self._again  # type: ignore[method-assign]
        self.add_item(again)
        self.add_item(home_button(bot, row=1))

    def render(self) -> str:
        return (
            "## No manageable servers found\n"
            "Discord did not report any server where this account is the owner, an Administrator, "
            "or has **Manage Server**.\n\n"
            "If you used the wrong Discord account, or you have just been given the permission, "
            "press **Verify Again**."
        )

    async def show(self, interaction: discord.Interaction) -> None:
        await reply(interaction, self.render(), view=self)

    async def _again(self, interaction: discord.Interaction) -> None:
        self.stop()
        await start_verification(interaction)


def verified_guild_info(guild: VerifiedGuild):
    """The same shape guild_info() gives for a live guild, from verified data."""
    from bot.services.listings import GuildInfo

    return GuildInfo(
        guild_id=guild.id, name=guild.name, icon_url=guild.icon_url, member_count=guild.member_count
    )

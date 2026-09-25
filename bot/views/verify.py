"""“Verify My Servers”: the botless route into the listing form.

Listing a server never requires installing Parley. A read-only Discord login
proves which servers the user manages. The browser callback then refreshes the
same Discord message automatically, so there is no extra Continue step.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from bot.services import verification
from bot.services.verification import VerifiedGuild
from bot.utils.helpers import utcnow
from bot.views.base import OwnedView, acknowledge, get_bot, home_button, reply, edit_response

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

TITLE = "🌟 Post Your Server"
DESCRIPTION = "Pick a server to list.\n\n**No bot required.**"
UNAVAILABLE = "Server verification is not ready yet. Please try again shortly."


def _decorate(embed: discord.Embed, bot: ParleyBot) -> discord.Embed:
    """Give the compact flow a little Parley identity without visual clutter."""
    user = getattr(bot, "user", None)
    avatar = getattr(user, "display_avatar", None)
    url = getattr(avatar, "url", None)
    if url:
        embed.set_thumbnail(url=str(url))
    return embed


async def _show(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    """Keep the full verification journey inside one ephemeral Discord message."""
    if interaction.response.is_done():
        kwargs = {"content": content, "view": view}
        if embed is None:
            kwargs["embeds"] = []
        else:
            kwargs["embed"] = embed
        await interaction.edit_original_response(**kwargs)
        return
    if interaction.message is not None and (interaction.guild is None or interaction.message.flags.ephemeral):
        kwargs = {"content": content, "view": view}
        if embed is None:
            kwargs["embeds"] = []
        else:
            kwargs["embed"] = embed
        await edit_response(interaction, **kwargs)
        return
    await reply(interaction, content, embed=embed, view=view)


async def start_verification(
    interaction: discord.Interaction,
    *,
    notice: str | None = None,
    force: bool = False,
) -> None:
    """Show the one-click verification card, or reuse a recent verification."""
    bot = get_bot(interaction)
    if not bot.settings.oauth_enabled or not (bot.settings.oauth_client_id or bot.application_id):
        embed = _decorate(
            discord.Embed(
                title=TITLE,
                description=UNAVAILABLE,
                color=bot.runtime.bot.color_warning,
            ),
            bot,
        )
        await _show(interaction, embed=embed)
        return

    if not force:
        async with bot.db.session() as session:
            recent = await verification.latest_verification(
                session, user_id=interaction.user.id, now=utcnow()
            )
        if recent.completed:
            await show_verified_servers(interaction)
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
    await _show(interaction, embed=view.embed(notice), view=view)

    oauth = getattr(bot, "oauth", None)
    watch = getattr(oauth, "watch", None)
    if callable(watch):
        watch(state, interaction)


class VerifyView(OwnedView):
    """A friendly one-button entry into botless server listing."""

    def __init__(self, bot: ParleyBot, owner_id: int, url: str) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.add_item(discord.ui.Button(label="Choose My Server", emoji="✨", url=url, row=0))

    def embed(self, notice: str | None = None) -> discord.Embed:
        description = DESCRIPTION
        if notice:
            description = f"{notice}\n\n{description}"
        embed = _decorate(
            discord.Embed(
                title=TITLE,
                description=description,
                color=self.bot.runtime.bot.color_primary,
            ),
            self.bot,
        )
        embed.set_footer(text="This message updates automatically after verification.")
        return embed


async def show_verified_servers(interaction: discord.Interaction) -> None:
    """Replace the verification card with the servers Discord verified."""

    bot = get_bot(interaction)
    async with bot.db.session() as session:
        result = await verification.latest_verification(
            session, user_id=interaction.user.id, now=utcnow()
        )
    guilds = result.guilds

    if not guilds:
        if result.completed:
            await NoServersView(bot, interaction.user.id).show(interaction)
            return
        await start_verification(
            interaction,
            notice="Verification hasn't finished yet. Open **Choose My Server** to continue.",
            force=True,
        )
        return

    view = VerifiedGuildPickerView(bot, interaction.user.id, guilds)
    embed = _decorate(
        discord.Embed(
            title="😊 Choose a Server",
            description="Pick a server. If a new one is missing, refresh the list.",
            color=bot.runtime.bot.color_success,
        ),
        bot,
    )
    await _show(interaction, embed=embed, view=view)


class VerifiedGuildPickerView(OwnedView):
    """The compact second step: one dropdown and nothing else."""

    def __init__(self, bot: ParleyBot, owner_id: int, guilds: list[VerifiedGuild]) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.guilds = {guild.id: guild for guild in guilds}
        select = discord.ui.Select(
            placeholder="Choose a server",
            options=[
                discord.SelectOption(label=guild.name[:100], value=str(guild.id))
                for guild in guilds[:25]
            ],
        )
        select.callback = self._picked  # type: ignore[method-assign]
        self._select = select
        self.add_item(select)

        refresh = discord.ui.Button(
            label="Refresh Servers", emoji="🔄", style=discord.ButtonStyle.secondary, row=1
        )
        refresh.callback = self._refresh  # type: ignore[method-assign]
        self.add_item(refresh)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        """Run Discord verification again so newly-created servers appear."""
        await acknowledge(interaction, thinking=False)
        self.stop()
        await start_verification(interaction, force=True)

    async def _picked(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction, thinking=False)
        guild_id = int(self._select.values[0])
        chosen = self.guilds.get(guild_id)
        if chosen is None:
            await start_verification(interaction, notice="Please choose a server again.")
            return
        from bot.views.listings import open_verified_listing_form

        await open_verified_listing_form(interaction, chosen)


class NoServersView(OwnedView):
    """Verification finished, but this account manages nothing Parley can list."""

    def __init__(self, bot: ParleyBot, owner_id: int) -> None:
        super().__init__(owner_id)
        self.bot = bot
        again = discord.ui.Button(label="Check Again", emoji="✨", style=discord.ButtonStyle.primary, row=0)
        again.callback = self._again  # type: ignore[method-assign]
        self.add_item(again)
        self.add_item(home_button(bot, row=1))

    def embed(self) -> discord.Embed:
        return _decorate(
            discord.Embed(
                title="😊 No servers found",
                description=(
                    "We couldn't find a server you can manage with this Discord account.\n\n"
                    "Try another account or check your **Manage Server** permission."
                ),
                color=self.bot.runtime.bot.color_warning,
            ),
            self.bot,
        )

    async def show(self, interaction: discord.Interaction) -> None:
        await _show(interaction, embed=self.embed(), view=self)

    async def _again(self, interaction: discord.Interaction) -> None:
        await acknowledge(interaction, thinking=False)
        self.stop()
        await start_verification(interaction, force=True)


def verified_guild_info(guild: VerifiedGuild):
    """The same shape guild_info() gives for a live guild, from verified data."""
    from bot.services.listings import GuildInfo

    return GuildInfo(
        guild_id=guild.id,
        name=guild.name,
        icon_url=guild.icon_url,
        member_count=guild.member_count,
    )

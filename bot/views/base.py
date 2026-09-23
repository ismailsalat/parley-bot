"""Shared UI plumbing: replying, error handling, spam guard, owned views."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from bot.config import templates
from bot.services import moderation, permissions
from bot.services.errors import ParleyError
from bot.utils.mentions import safe_allowed_mentions

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

GENERIC_ERROR = "Something went wrong on our side. Please try again in a moment."
MENU_TIMEOUT_SECONDS = 600  # transient menus; interaction tokens last 15 minutes


def get_bot(interaction: discord.Interaction) -> ParleyBot:
    return interaction.client  # type: ignore[return-value]


async def reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    embeds: list[discord.Embed] | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = True,
    file: discord.File | None = None,
) -> discord.Message | None:
    """Send a response or follow-up, whichever is valid right now. Never pings."""
    kwargs: dict[str, Any] = {"ephemeral": ephemeral, "allowed_mentions": safe_allowed_mentions()}
    if file is not None:
        kwargs["file"] = file
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if embeds:
        kwargs["embeds"] = embeds
    if view is not None:
        kwargs["view"] = view
    if interaction.response.is_done():
        return await interaction.followup.send(wait=True, **kwargs)
    await interaction.response.send_message(**kwargs)
    return None


async def handle_error(interaction: discord.Interaction, error: BaseException) -> None:
    """Friendly message for expected errors; log + generic message for bugs."""
    original = getattr(error, "original", error)
    if isinstance(original, app_commands.CheckFailure) and interaction.response.is_done():
        return  # the check already told the user why
    if isinstance(original, ParleyError):
        message = original.user_message
    elif isinstance(original, app_commands.NoPrivateMessage):
        message = "Please use this command inside a server."
    elif isinstance(original, app_commands.MissingPermissions):
        message = "You need Manage Server permission."
    elif isinstance(original, discord.Forbidden):
        log.warning("Discord refused an action (missing permission): %s", original)
        message = "Parley is missing a permission it needs for that. Please check its role and channel permissions."
    else:
        log.error("Unhandled interaction error", exc_info=original)
        message = GENERIC_ERROR
    try:
        await reply(interaction, f"⚠️ {message}")
    except discord.HTTPException as exc:
        log.warning("Could not deliver error message to user %s: %s", interaction.user.id, exc)


async def guard(interaction: discord.Interaction) -> bool:
    """Runs before every command and component: spam limit + blocked users."""
    bot = get_bot(interaction)
    if not bot.click_limiter.hit(interaction.user.id):
        await reply(interaction, "You're clicking a little fast. Please wait a few seconds.")
        return False
    # Production keeps database-backed user bans in memory so a slow/cold
    # database connection cannot consume Discord's interaction response window.
    # Test doubles without the cache retain the database-backed behavior.
    if hasattr(bot, "blocked_user_ids"):
        blocked = interaction.user.id in bot.blocked_user_ids
    else:
        async with bot.db.session() as session:
            blocked = await moderation.is_user_blocked(session, bot.runtime, interaction.user.id)
    if blocked:
        await reply(interaction, "You can't use Parley.")
        return False
    mode = bot.runtime.hub.mode
    if mode != "live" and not await permissions.is_staff_cached(bot, interaction.user.id):
        # TEST: only staff may try things. OFF: Parley is unavailable to users.
        await reply(interaction, templates.render(bot.runtime, "maintenance" if mode == "off" else "test_mode"))
        return False
    return True


class OwnedView(discord.ui.View):
    """A temporary menu that only the person who opened it can use."""

    def __init__(self, owner_id: int, *, timeout: float | None = MENU_TIMEOUT_SECONDS) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await reply(interaction, "This menu belongs to someone else.")
            return False
        return await guard(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await handle_error(interaction, error)


class ConfirmView(OwnedView):
    """Yes / Cancel confirmation for destructive actions."""

    def __init__(self, owner_id: int, *, confirm_label: str = "Confirm") -> None:
        super().__init__(owner_id, timeout=120)
        self.confirmed: bool | None = None
        self.interaction: discord.Interaction | None = None
        self.confirm.label = confirm_label

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        self.interaction = interaction
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = False
        self.interaction = interaction
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


# ---------------------------------------------------------------- Discord <-> service helpers


def guild_info(guild: discord.Guild):
    """Snapshot of live guild data. Discord context is trusted over user input."""
    from bot.services.listings import GuildInfo

    return GuildInfo(
        guild_id=guild.id,
        name=guild.name,
        icon_url=guild.icon.url if guild.icon else None,
        member_count=guild.member_count or 0,
    )


async def send_dm(bot: ParleyBot, user_id: int, **kwargs: Any) -> bool:
    """Best-effort DM. Returns False (and logs) when the user can't be reached."""
    kwargs.setdefault("allowed_mentions", safe_allowed_mentions())
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(**kwargs)
    except discord.Forbidden:
        log.info("Could not DM user %s (DMs closed or no shared server)", user_id)
        return False
    except discord.HTTPException as exc:
        log.warning("DM to user %s failed: %s", user_id, exc)
        return False
    return True


def user_label(bot: ParleyBot, user_id: int) -> str:
    """A mention plus a readable name, since mentions in DMs may not resolve."""
    user = bot.get_user(user_id)
    return f"<@{user_id}> ({user.name})" if user else f"<@{user_id}>"


async def deliver_dms(
    bot: ParleyBot, user_ids: list[int], *, actor_id: int | None, **kwargs: Any
) -> tuple[int, int]:
    """DM several users, respecting TEST/OFF mode. Returns (delivered, held_back).

    In LIVE mode everyone gets the message. Otherwise only staff receive it
    (marked 🧪) and the acting admin gets one copy of anything held back, so
    testing never surprises real users.
    """
    recipients = list(dict.fromkeys(user_ids))
    if bot.runtime.hub.mode == "live":
        results = [await send_dm(bot, uid, **kwargs) for uid in recipients]
        return sum(results), 0

    delivered = held = 0
    tagged = dict(kwargs)
    if tagged.get("content"):
        tagged["content"] = f"-# 🧪 TEST\n{tagged['content']}"[:2000]
    for uid in recipients:
        if await permissions.is_staff_cached(bot, uid):
            delivered += await send_dm(bot, uid, **tagged)
        else:
            held += 1
    if held and actor_id is not None and actor_id not in recipients:
        copy = dict(kwargs)
        note = f"-# 🧪 TEST mode: this was not sent to {held} non-staff user(s). Here is what they would see:"
        copy["content"] = f"{note}\n{kwargs.get('content') or ''}"[:2000]
        await send_dm(bot, actor_id, **copy)
    return delivered, held


def home_button(bot: ParleyBot, row: int | None = None) -> discord.ui.Item:
    """Grey navigation. Always place it on its own final row, never between actions."""
    from bot.views.welcome import action_button

    return action_button(bot, "home", style=discord.ButtonStyle.secondary, row=row)

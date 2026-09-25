"""Shared UI plumbing: replying, error handling, spam guard, owned views."""

from __future__ import annotations

import asyncio
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
# Leave a large safety margin under Discord's roughly three-second interaction
# acknowledgement deadline.  The watchdog is deliberately under one second:
# normal callbacks acknowledge immediately, so this only fires when dispatch,
# Postgres, or Discord itself is slower than expected.
ACK_WATCHDOG_SECONDS = 0.85
ACK_RECOVERY_SECONDS = 7.0


def get_bot(interaction: discord.Interaction) -> ParleyBot:
    return interaction.client  # type: ignore[return-value]


def arm_interaction_ack_watchdog(interaction: discord.Interaction) -> None:
    """Last-resort acknowledgement for every user interaction.

    Most Parley callbacks acknowledge immediately themselves. This watchdog is a
    safety net for a cold database connection, Discord API delay, or a newly
    added callback that accidentally does slow work first. If the callback has
    not responded after a short grace period, Parley defers the interaction so
    Discord does not show "This application did not respond".

    Component interactions use a deferred *message update* so existing menu
    code can still call ``edit_original_response``. Commands/modal submits use
    a normal ephemeral thinking response. Modal-opening buttons are unaffected
    because opening the modal completes the response before the watchdog fires.
    """
    if interaction.response.is_done():
        return
    # Unit-test doubles and non-Discord shims may not expose the wire-level
    # interaction id/type. Real Discord interactions always do.
    if not hasattr(interaction, "id") or not hasattr(interaction, "type"):
        return

    bot = get_bot(interaction)
    tasks = getattr(bot, "_interaction_ack_watchdogs", None)
    if tasks is None:
        tasks = {}
        setattr(bot, "_interaction_ack_watchdogs", tasks)
    if interaction.id in tasks:
        return

    started = asyncio.get_running_loop().time()

    async def _watch() -> None:
        try:
            await asyncio.sleep(ACK_WATCHDOG_SECONDS)
            if interaction.response.is_done():
                return
            is_component = interaction.type == discord.InteractionType.component
            data = getattr(interaction, "data", None) or {}
            custom_id = data.get("custom_id") if isinstance(data, dict) else None
            is_persistent_parley = is_component and isinstance(custom_id, str) and custom_id.startswith("wp:")
            await interaction.response.defer(
                # Persistent public buttons recover into a private response so
                # a watchdog can never overwrite the public directory/welcome
                # message itself. Transient component menus keep message-update
                # semantics for compatibility with edit_original_response().
                ephemeral=(not is_component) or is_persistent_parley,
                thinking=(not is_component) or is_persistent_parley,
            )
            log.warning(
                "interaction.ack_watchdog type=%s user_id=%s guild_id=%s elapsed=%.3fs",
                getattr(interaction.type, "name", interaction.type),
                interaction.user.id,
                getattr(interaction, "guild_id", None),
                asyncio.get_running_loop().time() - started,
            )

            # A defer prevents Discord's red timeout. Run the longer stalled-action
            # recovery in a separate task so the acknowledgement watchdog itself
            # is considered finished immediately after it successfully defers.
            async def _recover_stalled() -> None:
                try:
                    await asyncio.sleep(max(0.0, ACK_RECOVERY_SECONDS - ACK_WATCHDOG_SECONDS))
                    completed = getattr(bot, "_completed_interactions", set())
                    if interaction.id in completed:
                        return
                    message = getattr(interaction, "message", None)
                    channel_id = getattr(interaction, "channel_id", None)
                    owned_panel_message = (
                        message is not None
                        and getattr(getattr(message, "author", None), "id", None)
                        == getattr(getattr(bot, "user", None), "id", None)
                        and channel_id
                        in getattr(getattr(bot, "panels", None), "panel_channel_ids", lambda: set())()
                    )
                    if not (
                        isinstance(custom_id, str)
                        and (custom_id.startswith("wp:") or owned_panel_message)
                    ):
                        return
                    try:
                        from bot.views.welcome import action_button, persistent_view

                        await interaction.edit_original_response(
                            content=(
                                "Parley recovered a slow button response. The original action did not finish, "
                                "so use a fresh control below."
                            ),
                            embeds=[],
                            view=persistent_view(
                                action_button(bot, "find", row=0),
                                action_button(bot, "home", row=0),
                            ),
                            allowed_mentions=safe_allowed_mentions(),
                        )
                        log.error(
                            "interaction.recovered_stalled custom_id=%s user_id=%s guild_id=%s",
                            custom_id,
                            interaction.user.id,
                            getattr(interaction, "guild_id", None),
                        )
                    except discord.HTTPException as exc:
                        log.warning("Could not recover stalled interaction %s: %s", interaction.id, exc)
                except asyncio.CancelledError:
                    raise
                finally:
                    recoveries = getattr(bot, "_interaction_recovery_tasks", None)
                    if recoveries is not None:
                        recoveries.pop(interaction.id, None)

            recoveries = getattr(bot, "_interaction_recovery_tasks", None)
            if recoveries is None:
                recoveries = {}
                setattr(bot, "_interaction_recovery_tasks", recoveries)
            previous = recoveries.get(interaction.id)
            if previous is None or previous.done():
                recoveries[interaction.id] = asyncio.create_task(
                    _recover_stalled(), name=f"parley-interaction-recovery-{interaction.id}"
                )
        except (discord.HTTPException, discord.InteractionResponded):
            # Another callback response won the race, or Discord no longer accepts
            # the interaction. Either case needs no user-visible second error.
            pass
        finally:
            tasks.pop(interaction.id, None)

    tasks[interaction.id] = asyncio.create_task(
        _watch(), name=f"parley-interaction-ack-{interaction.id}"
    )


def mark_interaction_complete(interaction: discord.Interaction) -> None:
    """Tell the watchdog that a persistent interaction finished normally."""
    if not hasattr(interaction, "id"):
        return
    bot = get_bot(interaction)
    completed = getattr(bot, "_completed_interactions", None)
    if completed is None:
        completed = set()
        setattr(bot, "_completed_interactions", completed)
    completed.add(interaction.id)
    # The set is only a short-lived race marker. Bound it so a 24/7 process
    # cannot grow forever even under very heavy button traffic.
    if len(completed) > 20_000:
        completed.clear()
        completed.add(interaction.id)


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


async def edit_response(interaction: discord.Interaction, **kwargs: Any) -> discord.InteractionMessage | None:
    """Edit the component message whether or not a watchdog already deferred it."""
    kwargs.setdefault("allowed_mentions", safe_allowed_mentions())
    if interaction.response.is_done():
        return await interaction.edit_original_response(**kwargs)
    await interaction.response.edit_message(**kwargs)
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


async def guard(interaction: discord.Interaction, *, arm_watchdog: bool = True) -> bool:
    """Runs before every command and component: spam limit + blocked users."""
    if arm_watchdog:
        arm_interaction_ack_watchdog(interaction)
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
        await edit_response(interaction, content="Cancelled.", view=None)
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


async def acknowledge(interaction: discord.Interaction, *, thinking: bool = True) -> None:
    """Tell Discord we heard the click, before doing anything slow.

    Discord drops a component interaction after about three seconds, so any
    callback that reads the database must land here first. Safe to call twice.
    Never call it before opening a modal: a modal needs a fresh interaction.
    """
    # Arm the backup *before* the network request. Previously the watchdog was
    # started later by guard(), which meant a hung first defer had no safety net.
    arm_interaction_ack_watchdog(interaction)
    if not interaction.response.is_done():
        started = asyncio.get_running_loop().time()
        try:
            await interaction.response.defer(ephemeral=interaction.guild is not None, thinking=thinking)
            interaction_type = getattr(interaction, "type", None)
            log.debug(
                "interaction.ack type=%s user_id=%s guild_id=%s elapsed=%.3fs",
                getattr(interaction_type, "name", interaction_type),
                interaction.user.id,
                getattr(interaction, "guild_id", None),
                asyncio.get_running_loop().time() - started,
            )
        except discord.InteractionResponded:
            # The watchdog or a concurrent safe dispatcher won the race.
            pass


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

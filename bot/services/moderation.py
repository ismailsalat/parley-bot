"""Bans, blocks, blocked words and in-memory click rate limiting."""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.runtime import RuntimeConfig
from bot.database import repository
from bot.database.models import Listing, ListingStatus, RequestStatus, utcnow
from bot.services.errors import Banned, NotFound, ValidationError

GUILD = "guild"
USER = "user"


async def is_guild_banned(session: AsyncSession, config: RuntimeConfig, guild_id: int) -> bool:
    if guild_id in config.moderation.blocked_guild_ids:
        return True
    return await repository.get_ban(session, GUILD, guild_id) is not None


async def is_user_blocked(session: AsyncSession, config: RuntimeConfig, user_id: int) -> bool:
    if user_id in config.moderation.blocked_user_ids:
        return True
    return await repository.get_ban(session, USER, user_id) is not None


async def ensure_allowed(
    session: AsyncSession, config: RuntimeConfig, *, guild_ids: Iterable[int] = (), user_id: int | None = None
) -> None:
    if user_id is not None and await is_user_blocked(session, config, user_id):
        raise Banned("You can't use Waypoint.")
    for guild_id in guild_ids:
        if await is_guild_banned(session, config, guild_id):
            raise Banned("This server can't use the Waypoint network.")


def find_blocked_word(text: str, blocked_words: Iterable[str]) -> str | None:
    """Whole-word, case-insensitive match (so 'class' does not match 'ass')."""
    lowered = text.lower()
    for word in blocked_words:
        word = word.strip().lower()
        if word and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", lowered):
            return word
    return None


def ensure_clean(text: str, config: RuntimeConfig) -> None:
    if find_blocked_word(text, config.moderation.blocked_words):
        raise ValidationError("Your text contains a word that isn't allowed on Waypoint. Please edit it and try again.")


async def _take_down_listing(session: AsyncSession, guild_id: int, status: str) -> Listing | None:
    listing = await repository.get_listing(session, guild_id)
    if listing is not None:
        listing.status = status
        listing.pending_changes = None
    return listing


async def _cancel_pending_requests(session: AsyncSession, guild_id: int) -> int:
    requests = await repository.pending_requests_involving(session, guild_id)
    for request in requests:
        request.status = RequestStatus.CANCELLED
        request.responded_at = utcnow()
    return len(requests)


async def ban_guild(session: AsyncSession, *, guild_id: int, reason: str | None, moderator_id: int) -> Listing | None:
    """Ban by Guild ID. Returns the listing (if any) so its message can be deleted."""
    await repository.add_ban(session, GUILD, guild_id, reason, moderator_id)
    listing = await _take_down_listing(session, guild_id, ListingStatus.REMOVED)
    guild = await repository.get_guild(session, guild_id)
    if guild is not None:
        guild.status = "banned"
    network = await repository.get_network_settings(session, guild_id)
    if network is not None:
        network.enabled = False
    cancelled = await _cancel_pending_requests(session, guild_id)
    await repository.add_audit(
        session, "moderation.ban_guild", actor_id=moderator_id, guild_id=guild_id,
        details={"reason": reason, "cancelled_requests": cancelled},
    )
    return listing


async def unban_guild(session: AsyncSession, *, guild_id: int, moderator_id: int) -> None:
    if not await repository.remove_ban(session, GUILD, guild_id):
        raise NotFound("That server is not banned.")
    guild = await repository.get_guild(session, guild_id)
    if guild is not None and guild.status == "banned":
        guild.status = "active"
    await repository.add_audit(session, "moderation.unban_guild", actor_id=moderator_id, guild_id=guild_id)


async def block_user(session: AsyncSession, *, user_id: int, reason: str | None, moderator_id: int) -> None:
    await repository.add_ban(session, USER, user_id, reason, moderator_id)
    await repository.add_audit(
        session, "moderation.block_user", actor_id=moderator_id, details={"user_id": user_id, "reason": reason}
    )


async def unblock_user(session: AsyncSession, *, user_id: int, moderator_id: int) -> None:
    if not await repository.remove_ban(session, USER, user_id):
        raise NotFound("That user is not blocked.")
    await repository.add_audit(session, "moderation.unblock_user", actor_id=moderator_id, details={"user_id": user_id})


async def suspend_listing(
    session: AsyncSession, *, guild_id: int, reason: str | None, moderator_id: int, restore: bool = False
) -> Listing:
    listing = await repository.get_listing(session, guild_id)
    if listing is None or listing.status in (ListingStatus.REMOVED,):
        raise NotFound("That server has no listing.")
    if restore:
        if listing.status != ListingStatus.SUSPENDED:
            raise ValidationError("That listing is not suspended.")
        listing.status = ListingStatus.ACTIVE
        listing.refreshed_at = utcnow()
    else:
        if listing.status == ListingStatus.SUSPENDED:
            raise ValidationError("That listing is already suspended.")
        listing.status = ListingStatus.SUSPENDED
        listing.pending_changes = None
        await _cancel_pending_requests(session, guild_id)
    await repository.add_audit(
        session,
        "moderation.restore_listing" if restore else "moderation.suspend_listing",
        actor_id=moderator_id, guild_id=guild_id, details={"reason": reason},
    )
    return listing


async def staff_remove_listing(session: AsyncSession, *, guild_id: int, reason: str | None, moderator_id: int) -> Listing:
    listing = await repository.get_listing(session, guild_id)
    if listing is None or listing.status == ListingStatus.REMOVED:
        raise NotFound("That server has no listing.")
    listing.status = ListingStatus.REMOVED
    listing.pending_changes = None
    await _cancel_pending_requests(session, guild_id)
    await repository.add_audit(
        session, "moderation.remove_listing", actor_id=moderator_id, guild_id=guild_id, details={"reason": reason}
    )
    return listing


class RateLimiter:
    """Sliding-window limiter for button spam.

    Deliberately in memory: it only smooths out rapid clicking. Anything that
    must survive restarts (refresh/request cooldowns) is stored in the database.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[int, deque[float]] = {}

    def configure(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds

    def hit(self, key: int, now: float | None = None) -> bool:
        """Record a hit. Returns False when the key is over the limit."""
        now = time.monotonic() if now is None else now
        bucket = self._hits.setdefault(key, deque())
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        if len(self._hits) > 10_000:  # keep memory bounded
            for stale in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
                del self._hits[stale]
        return True

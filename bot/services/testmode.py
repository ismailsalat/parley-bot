"""TEST mode support: test listings, Test Center messages and going live.

In TEST mode only staff can use Parley, network ads are paused, DMs to
non-staff users are held back (the acting admin gets a copy instead) and new
listings are flagged ``is_test`` so they never appear to real users after
Parley goes LIVE unless the owner explicitly keeps them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import and_, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database import repository
from bot.services import cooldowns
from bot.services.errors import ValidationError
from bot.database.models import Cooldown, ListingContact, ListingStatus, RequestStatus, utcnow

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

TEST_LABEL = "🧪 TEST"
TEST_LISTING_NOTE = "-# 🧪 TEST listing: only visible while Parley is in test mode"

SAMPLE_AD = (
    "# 🌙 Example Café\n"
    "A cozy **community** server for chatting, games and events.\n\n"
    "## What we offer\n"
    "- 🎮 Weekly game nights\n"
    "- 🎨 Art & music channels\n"
    "- 🤝 Friendly staff\n\n"
    "> *Everyone is welcome!* `@everyone` pings are never sent by Parley."
)


async def record_message(session: AsyncSession, *, channel_id: int, message_id: int, kind: str) -> None:
    await repository.add_test_message(session, channel_id=channel_id, message_id=message_id, kind=kind)


async def delete_test_messages(bot: ParleyBot) -> int:
    """Delete every message the Test Center posted. Returns how many were removed."""
    async with bot.db.session() as session:
        rows = await repository.test_messages(session)
    removed = []
    for row in rows:
        await bot.panels.delete_listing_message(row.channel_id, row.message_id)  # tolerant of already-deleted
        removed.append(row.id)
    async with bot.db.session() as session:
        await repository.delete_test_message_rows(session, removed)
    log.info("testmode.deleted_messages count=%s", len(removed))
    return len(removed)


async def discard_test_listings(session: AsyncSession) -> list[tuple[int, int | None, int | None]]:
    """Remove test listings completely. Returns (guild_id, channel_id, message_id) for message cleanup."""
    removed = []
    for listing in await repository.test_listings(session):
        removed.append((listing.guild_id, listing.channel_id, listing.message_id))
        for request in await repository.pending_requests_involving(session, listing.guild_id):
            request.status = RequestStatus.CANCELLED
            request.responded_at = utcnow()
        await session.execute(delete(ListingContact).where(ListingContact.guild_id == listing.guild_id))
        await session.delete(listing)  # AsyncSession.delete is a coroutine
    await session.flush()
    return removed


async def keep_test_listings(session: AsyncSession) -> list[int]:
    """Turn test listings into real ones. Returns guild IDs whose messages need re-rendering."""
    kept = []
    for listing in await repository.test_listings(session):
        listing.is_test = False
        if listing.status == ListingStatus.ACTIVE:
            kept.append(listing.guild_id)
    return kept


async def go_live(bot: ParleyBot, *, keep_listings: bool, actor_id: int) -> str:
    """Switch to LIVE. Test listings are removed unless ``keep_listings``."""
    from bot.services import configuration

    async with bot.db.session() as session:
        if keep_listings:
            refresh = await keep_test_listings(session)
            removed = []
        else:
            removed = await discard_test_listings(session)
            refresh = []
        await configuration.set_mode(session, "live", actor_id=actor_id)
        await repository.add_audit(
            session, "mode.live", actor_id=actor_id, details={"kept": len(refresh), "removed": len(removed)}
        )
    await bot.reload_runtime_config()
    for _gid, channel_id, message_id in removed:
        await bot.panels.delete_listing_message(channel_id, message_id)
    for guild_id in refresh:
        await bot.panels.update_listing_message(guild_id)
    deleted = await delete_test_messages(bot)
    log.info("mode.live actor_id=%s kept=%s removed=%s test_messages=%s", actor_id, len(refresh), len(removed), deleted)
    parts = ["🟢 **Parley is LIVE.**"]
    if removed:
        parts.append(f"Removed {len(removed)} test listing(s).")
    if refresh:
        parts.append(f"Kept {len(refresh)} test listing(s) as real listings.")
    if deleted:
        parts.append(f"Cleaned up {deleted} test message(s).")
    return " ".join(parts)


async def reset_test_cooldowns(session: AsyncSession, *, actor_id: int) -> tuple[int, int]:
    """Clear wait timers used while staff are testing.

    Only TEST listings are touched, plus the acting staff member's request
    cooldown. This keeps the shortcut useful without wiping unrelated live-user
    anti-spam state if a bot was switched from LIVE back into TEST mode.
    """
    listings = list(await repository.test_listings(session))
    guild_ids = [listing.guild_id for listing in listings]
    for listing in listings:
        listing.refreshed_at = None
        listing.last_ad_edit_at = None

    conditions = [
        and_(Cooldown.scope == cooldowns.SCOPE_REQUEST_USER, Cooldown.subject == str(actor_id))
    ]
    for guild_id in guild_ids:
        gid = str(guild_id)
        conditions.extend(
            [
                Cooldown.subject == gid,
                Cooldown.subject.like(f"{gid}:%"),
                Cooldown.subject.like(f"%:{gid}"),
            ]
        )
    result = await session.execute(delete(Cooldown).where(or_(*conditions)))
    await repository.add_audit(
        session,
        "test.cooldowns_reset",
        actor_id=actor_id,
        details={"test_listings": len(guild_ids), "cooldowns": result.rowcount or 0},
    )
    return len(guild_ids), result.rowcount or 0


async def reset_my_quick_listing(bot: ParleyBot, *, actor_id: int) -> bool:
    """Staff-only utility (enforced by its Settings page), for the caller alone.

    A Free Listing uses BOTH a database ownership slot and cooldown rows.
    The old Reset Cooldowns action cleared neither a live Free Listing's slot
    nor its quick_post_deleted cooldown. This deliberate, confirmed reset does
    both for the acting staff member and nobody else.

    Keep the removed listing as an audit tombstone: the existing create flow
    permits only that same submitter to restore the same guild later.
    """
    from bot.services import quick_post
    from bot.database.models import Cooldown

    removed = None
    async with bot.db.session() as session:
        row = await quick_post.my_quick_listing(session, actor_id)
        if row is not None:
            guild = await repository.get_guild(session, row.guild_id)
            if guild is not None and guild.connected_by is not None:
                raise ValidationError("This is a connected server; manage it from My Server Listings instead.")
            removed = await quick_post.delete_quick_listing(
                session, bot.runtime, actor_id=actor_id, guild_id=row.guild_id,
                now=utcnow(),
            )
            # A deleted listing also keeps its old refreshed_at timestamp;
            # clear it so the acting staff member can actually test posting
            # the *same* guild again, not merely a different one.
            removed.refreshed_at = None
            removed.last_ad_edit_at = None
        await session.execute(
            delete(Cooldown).where(
                Cooldown.scope == "quick_post_deleted", Cooldown.subject == str(actor_id)
            )
        )
        # Don't replace the guild-specific quick_deleted audit action: that
        # action is needed to safely restore a previously deleted guild.
        await repository.add_audit(
            session, "test.my_free_listing_reset", actor_id=actor_id,
            details={"own_guild_id": removed.guild_id if removed else None},
        )

    if removed is not None:
        try:
            await bot.panels.take_down_listing(removed)
        except Exception:
            log.exception("Couldn't immediately remove reset listing %s; cleanup will retry", removed.guild_id)
    return removed is not None


async def clear_test_listings(bot: ParleyBot, *, actor_id: int) -> int:
    """Hard-delete every TEST listing and its Discord-owned helper messages.

    This exists so staff can repeat the first-time listing flow without waiting
    for cooldowns or manually cleaning rows. It is intentionally unavailable in
    LIVE mode and never removes a non-test listing.
    """
    if bot.runtime.hub.mode != "test":
        raise ValidationError("Clear Test Listings is only available while Parley is in TEST mode.")

    # Close any temporary self-post permission windows first so a hard reset can
    # never leave a member with Send Messages in the locked directory.
    from bot.views import self_post

    await self_post.restore_abandoned_sessions(bot)

    async with bot.db.session() as session:
        rows = list(await repository.test_listings(session))
        guild_ids = [listing.guild_id for listing in rows]
        refs = [
            (listing.channel_id, listing.message_id)
            for listing in rows
            if listing.channel_id and listing.message_id
        ]
        refs += [
            (listing.channel_id, listing.controls_message_id)
            for listing in rows
            if listing.channel_id and listing.controls_message_id
        ]
        refs += [
            (listing.review_channel_id, listing.review_message_id)
            for listing in rows
            if listing.review_channel_id and listing.review_message_id
        ]
        refs += [
            (listing.partner_channel_id, listing.partner_message_id)
            for listing in rows
            if listing.partner_channel_id and listing.partner_message_id
        ]
        refs += [
            (listing.partner_channel_id, listing.partner_controls_message_id)
            for listing in rows
            if listing.partner_channel_id and listing.partner_controls_message_id
        ]
        if guild_ids:
            cooldown_conditions = []
            for guild_id in guild_ids:
                gid = str(guild_id)
                cooldown_conditions.extend(
                    [
                        Cooldown.subject == gid,
                        Cooldown.subject.like(f"{gid}:%"),
                        Cooldown.subject.like(f"%:{gid}"),
                    ]
                )
            await session.execute(delete(Cooldown).where(or_(*cooldown_conditions)))
        await discard_test_listings(session)
        await repository.add_audit(
            session, "test.listings_cleared", actor_id=actor_id, details={"count": len(rows)}
        )

    for channel_id, message_id in dict.fromkeys(refs):
        await bot.panels.delete_listing_message(channel_id, message_id)
    log.info("testmode.cleared_listings actor_id=%s count=%s", actor_id, len(rows))
    return len(rows)

"""Moderation: Guild ID bans, user blocks, blocked words, rate limiting."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from bot.database import repository
from bot.database.models import ListingStatus, RequestStatus
from bot.services import moderation, partnerships
from bot.services.errors import Banned, NotFound, ValidationError
from tests.factories import NOW, make_listing

GID, OTHER = 600000000000000001, 600000000000000002
STAFF = 42


async def test_guild_ban_takes_everything_down(db, config):
    async with db.session() as session:
        await make_listing(session, config, GID)
        await make_listing(session, config, OTHER, actor_id=77)
        request = (
            await partnerships.create_request(
                session, config, source_guild_id=OTHER, target_guild_id=GID, requester_id=77,
                source_member_count=500, message=None, now=NOW,
            )
        ).request
    async with db.session() as session:
        listing = await moderation.ban_guild(session, guild_id=GID, reason="scam", moderator_id=STAFF)
        assert listing.status == ListingStatus.REMOVED
    async with db.session() as session:
        assert await moderation.is_guild_banned(session, config, GID)
        assert (await repository.get_request(session, request.id)).status == RequestStatus.CANCELLED
        assert (await repository.recent_audit(session, GID))[0].action == "moderation.ban_guild"


async def test_banned_guild_cannot_relist_with_a_new_invite(db, config):
    async with db.session() as session:
        await moderation.ban_guild(session, guild_id=GID, reason=None, moderator_id=STAFF)
    with pytest.raises(Banned):
        async with db.session() as session:
            await make_listing(session, config, GID, invite_url="https://discord.gg/brandnew")


async def test_unban_allows_listing_again(db, config):
    async with db.session() as session:
        await moderation.ban_guild(session, guild_id=GID, reason=None, moderator_id=STAFF)
        await moderation.unban_guild(session, guild_id=GID, moderator_id=STAFF)
    async with db.session() as session:
        assert not await moderation.is_guild_banned(session, config, GID)
        await make_listing(session, config, GID)
    with pytest.raises(NotFound):
        async with db.session() as session:
            await moderation.unban_guild(session, guild_id=GID, moderator_id=STAFF)


async def test_banning_twice_does_not_duplicate(db, config):
    async with db.session() as session:
        await moderation.ban_guild(session, guild_id=GID, reason="a", moderator_id=STAFF)
    async with db.session() as session:
        await moderation.ban_guild(session, guild_id=GID, reason="b", moderator_id=STAFF)
        ban = await repository.get_ban(session, moderation.GUILD, GID)
        assert ban is not None


async def test_config_blocklist_also_bans(db, config):
    config = replace(config, moderation=replace(config.moderation, blocked_guild_ids=(GID,)))
    with pytest.raises(Banned):
        async with db.session() as session:
            await make_listing(session, config, GID)


async def test_blocked_user_cannot_create_or_request(db, config):
    async with db.session() as session:
        await moderation.block_user(session, user_id=1000, reason="abuse", moderator_id=STAFF)
    with pytest.raises(Banned, match="You can't use Waypoint."):
        async with db.session() as session:
            await make_listing(session, config, GID, actor_id=1000)
    async with db.session() as session:
        await moderation.unblock_user(session, user_id=1000, moderator_id=STAFF)
        assert not await moderation.is_user_blocked(session, config, 1000)


async def test_suspend_and_restore(db, config):
    async with db.session() as session:
        await make_listing(session, config, GID)
        await moderation.suspend_listing(session, guild_id=GID, reason="check", moderator_id=STAFF)
    async with db.session() as session:
        assert (await repository.get_listing(session, GID)).status == ListingStatus.SUSPENDED
        restored = await moderation.suspend_listing(session, guild_id=GID, reason=None, moderator_id=STAFF, restore=True)
        assert restored.status == ListingStatus.ACTIVE


async def test_staff_remove(db, config):
    async with db.session() as session:
        await make_listing(session, config, GID)
        await moderation.staff_remove_listing(session, guild_id=GID, reason=None, moderator_id=STAFF)
    with pytest.raises(NotFound):
        async with db.session() as session:
            await moderation.staff_remove_listing(session, guild_id=GID, reason=None, moderator_id=STAFF)


def test_blocked_words_match_whole_words_only():
    assert moderation.find_blocked_word("Buy cheap nitro now", ["nitro"]) == "nitro"
    assert moderation.find_blocked_word("a classy server", ["ass"]) is None
    assert moderation.find_blocked_word("anything", []) is None


async def test_blocked_words_reject_ads(db, config):
    config = replace(config, moderation=replace(config.moderation, blocked_words=("scam",)))
    with pytest.raises(ValidationError, match="isn't allowed"):
        async with db.session() as session:
            await make_listing(session, config, GID, advertisement_text="Totally not a SCAM")


def test_rate_limiter_window():
    limiter = moderation.RateLimiter(limit=3, window_seconds=10)
    assert all(limiter.hit(1, now=t) for t in (0, 1, 2))
    assert not limiter.hit(1, now=3)
    assert limiter.hit(2, now=3)  # other users are unaffected
    assert limiter.hit(1, now=11)  # the window slid past the first click


async def test_cooldowns_survive_new_sessions(db, config):
    """Cooldowns live in the database, so a restart (new session) sees them."""
    from bot.services import cooldowns

    async with db.session() as session:
        await cooldowns.start(session, cooldowns.SCOPE_REQUEST_USER, 5, NOW, timedelta(minutes=2))
    async with db.session() as session:
        assert await cooldowns.remaining(session, cooldowns.SCOPE_REQUEST_USER, 5, NOW + timedelta(minutes=1))
        assert await repository.delete_expired_cooldowns(session, NOW + timedelta(minutes=3)) == 1

"""No-OAuth Quick Posts never confer verified server management rights."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from bot.database import repository
from bot.database.models import ListingStatus
from bot.services import listings, quick_post
from bot.services.errors import Conflict, CooldownActive, ValidationError

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def guild(gid: int) -> listings.GuildInfo:
    return listings.GuildInfo(guild_id=gid, name=f"Community {gid}", icon_url=None, member_count=100)


async def submit(session, config, *, gid=1001, user=2002, at=NOW):
    return await quick_post.create_quick_listing(
        session, config, info=guild(gid), actor_id=user,
        invite_url="https://discord.gg/testInvite", description="A friendly game server.",
        category="Gaming", now=at,
    )


async def test_quick_post_has_no_manager_or_partnership_authority(db, config):
    config = replace(config, listings=replace(config.listings, approval_required=True))
    async with db.session() as session:
        row, result = await submit(session, config)
        assert result == "pending"
        assert row.quick_submitted_by == 2002
        assert row.accepting_partnerships is False
        assert row.status == ListingStatus.PENDING
        stored = await repository.get_guild(session, 1001)
        assert stored.connected_by is None  # submitting an invite NEVER verifies a server
        assert await repository.get_contact_ids(session, 1001) == []

    async with db.session() as session:
        with pytest.raises(Conflict):
            await submit(session, config, gid=1002)  # 1 free slot per user
        with pytest.raises(Conflict):
            await submit(session, config, gid=1001)  # pending review, no re-submit
        with pytest.raises(Conflict):
            await submit(session, config, gid=1001, user=3003)  # cannot hijack another listing


async def test_quick_relist_24_hours_after_approval(db, config):
    config = replace(config, listings=replace(config.listings, approval_required=True))
    async with db.session() as session:
        await submit(session, config)
        await listings.review_listing(
            session, guild_id=1001, approve=True, moderator_id=4444,
            now=NOW + timedelta(minutes=15), revision=1,
        )
    async with db.session() as session:
        with pytest.raises(CooldownActive):
            await submit(session, config, at=NOW + timedelta(hours=23))
        row, action = await submit(session, config, at=NOW + timedelta(hours=25))
        assert action == "reposted"
        assert row.status == ListingStatus.ACTIVE
        assert row.quick_submitted_by == 2002


async def test_rejected_quick_post_can_be_corrected_next_day(db, config):
    config = replace(config, listings=replace(config.listings, approval_required=True))
    async with db.session() as session:
        await submit(session, config)
        await listings.review_listing(
            session, guild_id=1001, approve=False, moderator_id=4444,
            now=NOW + timedelta(minutes=1), revision=1,
        )
    async with db.session() as session:
        with pytest.raises(CooldownActive):
            await submit(session, config, at=NOW + timedelta(minutes=30))
        revised, action = await submit(session, config, at=NOW + timedelta(days=1))
        assert (action, revised.status) == ("pending", ListingStatus.PENDING)
        assert revised.review_revision == 2


def test_quick_descriptions_block_links_and_mentions(config):
    for bad in ("https://evil.example", "@everyone join", "<@12345> hello", ""):
        with pytest.raises(ValidationError):
            quick_post.quick_description(bad, config)
    assert quick_post.quick_description("We're a casual gaming club!", config)


async def test_quick_auto_publish_without_oauth(db, config):
    async with db.session() as session:
        row, outcome = await submit(session, config, gid=4001, user=9001)
        assert (outcome, row.status) == ("published", ListingStatus.ACTIVE)
        assert row.pending_submitted_by is None
        assert (await repository.get_guild(session, 4001)).connected_by is None


async def test_quick_edit_does_not_reset_relist_cooldown(db, config):
    async with db.session() as session:
        row, _ = await submit(session, config, gid=4002, user=9002)
        original = row.refreshed_at
        result, outcome = await quick_post.edit_quick_listing(
            session, config, guild_id=4002, actor_id=9002,
            category="Anime", info=guild(4002),
            invite_url="https://discord.gg/testInvite", description="New text!",
            now=NOW + timedelta(minutes=2),
        )
        assert outcome == "edited"
        assert result.refreshed_at == original
        assert result.category == "Anime"
        with pytest.raises(CooldownActive):
            await quick_post.edit_quick_listing(
                session, config, guild_id=4002, actor_id=9002,
                category="Gaming", info=guild(4002),
                invite_url="https://discord.gg/testInvite", description="Again!",
                now=NOW + timedelta(minutes=3),
            )


async def test_risky_link_needs_review_even_when_automatic(db, config):
    async with db.session() as session:
        row, result = await quick_post.create_quick_listing(
            session, config, info=guild(4003), actor_id=9003,
            invite_url="https://discord.gg/testInvite", description="",
            raw_ad="Join our gaming server! Website: https://example.org",
            category="Gaming", now=NOW,
        )
        assert (result, row.status) == ("pending", ListingStatus.PENDING)


def test_quick_pasted_ad_blocks_impersonation(db, config):
    info = guild(5000)
    for bad in ("@everyone join!", "<@12345> join", "https://discord.gg/different"):
        with pytest.raises(ValidationError):
            quick_post.prepare_quick_ad(
                config, info=info, invite_url="https://discord.gg/testInvite",
                category="Gaming", raw_ad=bad,
            )

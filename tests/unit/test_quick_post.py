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


async def test_free_delete_releases_one_listing_without_resetting_cooldown(db, config):
    async with db.session() as session:
        created, result = await submit(session, config, gid=6011, user=7011)
        assert result == "published"
    async with db.session() as session:
        removed = await quick_post.delete_quick_listing(
            session, config, actor_id=7011, guild_id=6011, now=NOW + timedelta(minutes=1),
        )
        assert removed.status == ListingStatus.REMOVED
        assert removed.quick_submitted_by is None
    async with db.session() as session:
        assert await quick_post.my_quick_listing(session, 7011) is None
        with pytest.raises(CooldownActive):
            await submit(session, config, gid=6012, user=7011, at=NOW + timedelta(hours=2))
        with pytest.raises(Conflict):
            await submit(session, config, gid=6011, user=7022, at=NOW + timedelta(hours=25))
    async with db.session() as session:
        second, result = await submit(session, config, gid=6012, user=7011, at=NOW + timedelta(hours=25))
        assert result == "published"
        assert second.quick_submitted_by == 7011


async def test_free_delete_revokes_pending_review_and_blocks_other_users(db, config):
    config = replace(config, listings=replace(config.listings, approval_required=True))
    async with db.session() as session:
        row, result = await submit(session, config, gid=6021, user=7031)
        assert result == "pending"
        with pytest.raises(Conflict):
            await quick_post.delete_quick_listing(
                session, config, actor_id=7032, guild_id=6021, now=NOW,
            )
    async with db.session() as session:
        await quick_post.delete_quick_listing(
            session, config, actor_id=7031, guild_id=6021, now=NOW + timedelta(minutes=1),
        )
    async with db.session() as session:
        with pytest.raises(Conflict):
            await listings.review_listing(
                session, guild_id=6021, approve=True, moderator_id=9999,
                now=NOW + timedelta(minutes=2), revision=1,
            )
        deleted = await repository.get_listing(session, 6021)
        assert deleted.status == ListingStatus.REMOVED
        assert deleted.pending_submitted_by is None


async def test_free_listing_menu_has_delete_and_verification_explanation(db, config):
    from bot.views.quick_post import show_quick_start
    from tests.fakes import FakeBot, FakeInteraction
    bot = FakeBot(db)
    async with db.session() as session:
        await submit(session, config, gid=6031, user=4)
    interaction = FakeInteraction(bot, 4)
    await show_quick_start(interaction)
    sent = interaction.response.sent[-1]
    assert "Unverified" in sent["content"]
    assert "Delete Listing" in [getattr(c, "label", None) for c in sent["view"].children]


async def test_only_original_submitter_can_restore_deleted_guild(db, config):
    async with db.session() as session:
        await submit(session, config, gid=6055, user=7111)
    async with db.session() as session:
        await quick_post.delete_quick_listing(
            session, config, actor_id=7111, guild_id=6055, now=NOW + timedelta(minutes=2),
        )
    async with db.session() as session:
        with pytest.raises(Conflict):
            await submit(session, config, gid=6055, user=9999, at=NOW + timedelta(hours=26))
        row, outcome = await submit(session, config, gid=6055, user=7111, at=NOW + timedelta(hours=26))
        assert (outcome, row.quick_submitted_by) == ("published", 7111)


def test_vanity_codes_and_invite_variants_use_one_server_identity(config):
    """An invite code/vanity slug is not a guild ID: Discord must resolve it."""
    from bot.services.listings import parse_invite_code

    assert parse_invite_code("camelot") == "camelot"
    assert parse_invite_code("discord.gg/camelot") == "camelot"
    assert parse_invite_code("https://discord.gg/camelot?utm_source=test") == "camelot"
    assert parse_invite_code("https://discord.com/invite/camelot") == "camelot"
    assert parse_invite_code("https://discordapp.com/invite/camelot") == "camelot"
    with pytest.raises(ValidationError):
        parse_invite_code("https://discord.gg.bad-domain.example/camelot")


async def test_pasted_ad_accepts_two_codes_and_vanity_for_same_guild(config):
    from types import SimpleNamespace

    checked = []

    class Bot:
        runtime = config

        async def fetch_invite(self, code, *, with_counts=False):
            checked.append(code)
            return SimpleNamespace(guild=SimpleNamespace(id=1001), code=code)

    raw = (
        "# 🏰 Camelot ⚔️\n"
        "https://discord.gg/Camelot\n"
        "https://discord.com/invite/alternate123?utm_source=parley"
    )
    validated = await quick_post.verify_advertisement_invites(
        Bot(), raw, guild_id=1001, invite_url="https://discord.gg/original123"
    )
    assert validated == frozenset({"Camelot", "alternate123"})
    assert checked == ["Camelot", "alternate123"]
    formatted, requires_review = quick_post.prepare_quick_ad(
        config, info=guild(1001), invite_url="https://discord.gg/original123",
        category="Gaming", raw_ad=raw, verified_invite_codes=validated,
    )
    assert formatted == raw  # the confirmed ad should not acquire a duplicate invite
    assert requires_review is False


async def test_vanity_only_pasted_ad_does_not_append_another_invite(config):
    from types import SimpleNamespace

    class Bot:
        runtime = config

        async def fetch_invite(self, code, *, with_counts=False):
            return SimpleNamespace(guild=SimpleNamespace(id=1001), code=code)

    raw = "Join Camelot: discord.gg/camelot"
    verified = await quick_post.verify_advertisement_invites(
        Bot(), raw, guild_id=1001, invite_url="https://discord.gg/otherCode"
    )
    text, _ = quick_post.prepare_quick_ad(
        config, info=guild(1001), invite_url="https://discord.gg/otherCode",
        category="Gaming", raw_ad=raw, verified_invite_codes=verified,
    )
    assert text == raw


async def test_different_server_invite_stays_blocked_even_if_vanity(config):
    from types import SimpleNamespace

    class Bot:
        runtime = config

        async def fetch_invite(self, code, *, with_counts=False):
            return SimpleNamespace(guild=SimpleNamespace(id=9999), code=code)

    with pytest.raises(ValidationError, match="different server"):
        await quick_post.verify_advertisement_invites(
            Bot(), "Visit https://discord.gg/unrelatedVanity",
            guild_id=1001, invite_url="https://discord.gg/original123",
        )
    with pytest.raises(ValidationError, match="additional invite"):
        quick_post.prepare_quick_ad(
            config, info=guild(1001), invite_url="https://discord.gg/original123",
            category="Gaming", raw_ad="https://discord.gg/unrelatedVanity",
        )


async def test_same_guild_invites_do_not_bypass_publish_time_gate(db, config):
    """Validated aliases must be explicitly passed to the database writer."""
    raw = "Join https://discord.gg/camelot"
    async with db.session() as session:
        with pytest.raises(ValidationError, match="additional invite"):
            await quick_post.create_quick_listing(
                session, config, info=guild(1234567), actor_id=7654321,
                invite_url="https://discord.gg/original123", description="",
                category="Gaming", raw_ad=raw, now=NOW,
            )
    async with db.session() as session:
        listing, outcome = await quick_post.create_quick_listing(
            session, config, info=guild(1234567), actor_id=7654321,
            invite_url="https://discord.gg/original123", description="",
            category="Gaming", raw_ad=raw, now=NOW,
            verified_invite_codes=frozenset({"camelot"}),
        )
        assert (outcome, listing.status) == ("published", ListingStatus.ACTIVE)
        assert listing.advertisement_text == raw

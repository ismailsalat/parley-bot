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


async def test_quick_approval_does_not_publish_or_start_cooldown_until_ad_posted(db, config):
    config = replace(config, listings=replace(config.listings, approval_required=True))
    async with db.session() as session:
        initial, state = await submit(session, config)
        assert state == "pending" and initial.awaiting_ad
        await listings.review_listing(
            session, guild_id=1001, approve=True, moderator_id=4444,
            now=NOW + timedelta(minutes=15), revision=1,
        )
    async with db.session() as session:
        approved = await repository.get_listing(session, 1001)
        assert approved.status == ListingStatus.ACTIVE and approved.awaiting_ad
        assert approved.refreshed_at is None
        with pytest.raises(Conflict):
            await submit(session, config, at=NOW + timedelta(hours=25))
        posted = await quick_post.publish_approved_ad(
            session, config, guild_id=1001, actor_id=2002, info=guild(1001),
            text="## Join our community!", verified_invite_codes=(),
            now=NOW + timedelta(hours=30),
        )
        assert posted.awaiting_ad is False
        assert posted.advertisement_text.startswith("## Join our community!")
    async with db.session() as session:
        with pytest.raises(CooldownActive):
            await submit(session, config, at=NOW + timedelta(hours=53))
        repost, action = await submit(session, config, at=NOW + timedelta(hours=55))
        assert action == "reposted" and repost.status == ListingStatus.ACTIVE


async def test_approved_ad_rejects_other_owner_and_external_links(db, config):
    config = replace(config, listings=replace(config.listings, approval_required=True))
    async with db.session() as session:
        await submit(session, config)
        await listings.review_listing(session, guild_id=1001, approve=True,
                                      moderator_id=4444, now=NOW, revision=1)
    async with db.session() as session:
        with pytest.raises(Conflict):
            await quick_post.publish_approved_ad(
                session, config, guild_id=1001, actor_id=9000, info=guild(1001),
                text="Hello", verified_invite_codes=(), now=NOW,
            )
        with pytest.raises(ValidationError):
            await quick_post.publish_approved_ad(
                session, config, guild_id=1001, actor_id=2002, info=guild(1001),
                text="https://some-website.example/", verified_invite_codes=(), now=NOW,
            )
        assert (await repository.get_listing(session, 1001)).awaiting_ad is True


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


# Parley 4 UX regression tests: these protect trust-first no-OAuth posting.

def test_age_only_language_is_not_classified_as_explicit(config):
    info = listings.GuildInfo(guild_id=94001, name="Camelot 18+ Gaming", icon_url=None, member_count=42)
    text, needs_review = quick_post.prepare_quick_ad(
        config, info=info, invite_url="https://discord.gg/Camelot",
        category="Gaming", raw_ad="18+ friends, weekend game nights! https://discord.gg/Camelot",
    )
    assert "18+" in text and needs_review is False
    with pytest.raises(ValidationError, match="NSFW"):
        quick_post.prepare_quick_ad(
            config, info=info, invite_url="https://discord.gg/Camelot",
            category="Gaming", raw_ad="NSFW server https://discord.gg/Camelot",
        )


async def test_platform_age_flag_routes_to_review_without_banning(db, config):
    # Discord age-restriction metadata is ambiguous; request review, don't ban.
    async with db.session() as session:
        row, action = await quick_post.create_quick_listing(
            session, config, info=guild(95001), actor_id=95002,
            invite_url="https://discord.gg/Camelot", category="Gaming",
            description="", raw_ad="Gaming friends: https://discord.gg/Camelot",
            additional_review=True, now=NOW,
        )
        assert action == "pending" and row.status == ListingStatus.PENDING
        assert row.awaiting_ad is False


def test_automatic_modal_only_asks_to_paste_one_ad(db, config):
    from bot.views.quick_post import QuickPostModal, QuickWizardView
    from tests.fakes import FakeBot
    bot = FakeBot(db, runtime=config)
    modal = QuickPostModal(bot, "Gaming")
    assert modal.intro_only is False
    assert len(modal.children) == 1
    assert "invite" in modal.children[0].label.lower()
    assert all("server invite" not in x.label.lower() for x in modal.children)
    wizard = QuickWizardView(bot, user_id=95002)
    assert len([x for x in wizard.children if getattr(x, "options", None)]) == 1
    assert "Write or Paste Ad" not in [getattr(x, "label", None) for x in wizard.children]


async def test_category_select_opens_modal_immediately(db, config):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from bot.views.quick_post import QuickWizardView, QuickPostModal
    from tests.fakes import FakeBot
    wizard = QuickWizardView(FakeBot(db, runtime=config), user_id=95002)
    response = SimpleNamespace(send_modal=AsyncMock())
    await wizard._pick(SimpleNamespace(data={"values": ["Gaming"]}, response=response))
    response.send_modal.assert_awaited_once()
    assert isinstance(response.send_modal.await_args.args[0], QuickPostModal)


async def test_staff_reset_only_clears_own_free_listing_and_cooldown(db, config):
    from types import SimpleNamespace
    from bot.services import testmode, cooldowns
    from tests.fakes import FakeBot
    bot = FakeBot(db, runtime=config)
    cleaned = []
    async def take_down(row):
        cleaned.append(row.guild_id)
    bot.panels = SimpleNamespace(take_down_listing=take_down)

    async with db.session() as session:
        await submit(session, config, gid=96001, user=96002)
        await submit(session, config, gid=96003, user=96004)
        await cooldowns.start(session, "quick_post_deleted", 96002, NOW, timedelta(hours=10))
        await cooldowns.start(session, "quick_post_deleted", 96004, NOW, timedelta(hours=10))

    assert await testmode.reset_my_quick_listing(bot, actor_id=96002) is True
    assert cleaned == [96001]
    async with db.session() as session:
        mine = await repository.get_listing(session, 96001)
        other = await repository.get_listing(session, 96003)
        assert mine.status == ListingStatus.REMOVED and mine.quick_submitted_by is None
        assert mine.refreshed_at is None and mine.last_ad_edit_at is None
        assert other.status == ListingStatus.ACTIVE and other.quick_submitted_by == 96004
        assert await cooldowns.remaining(session, "quick_post_deleted", 96002, NOW) is None
        assert await cooldowns.remaining(session, "quick_post_deleted", 96004, NOW) is not None
        # Same server can be submitted again by its original submitter.
        restored, state = await submit(session, config, gid=96001, user=96002, at=NOW+timedelta(minutes=5))
        assert state == "published" and restored.quick_submitted_by == 96002


def test_manual_approval_requires_invite_and_short_summary(db, config):
    from bot.views.quick_post import QuickPostModal
    from dataclasses import replace
    from tests.fakes import FakeBot
    manual = replace(config, listings=replace(config.listings, approval_required=True))
    modal = QuickPostModal(FakeBot(db, runtime=manual), "Gaming")
    assert modal.intro_only
    assert len(modal.children) == 2
    assert "invite" in modal.children[0].label.lower()
    assert "description" in modal.children[1].label.lower()

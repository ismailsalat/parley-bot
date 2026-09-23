"""Approval: edits to approved ads wait for staff, the old ad stays live, stale reviews are refused."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from bot.database import repository
from bot.database.models import ListingStatus
from bot.services import listings as listing_service
from bot.services import moderation
from bot.services.errors import Conflict
from tests.factories import NOW, OWNER, make_listing

GID = 700000000000000001
STAFF = 42


@pytest.fixture
def strict(config):
    return replace(config, listings=replace(config.listings, approval_required=True))


async def approved_listing(db, config) -> None:
    async with db.session() as session:
        listing = await make_listing(session, config, GID, advertisement_text="**Original ad**")
        assert listing.status == ListingStatus.PENDING
        revision = listing.review_revision
    async with db.session() as session:
        await listing_service.review_listing(session, guild_id=GID, approve=True, moderator_id=STAFF, now=NOW, revision=revision)


async def edit_ad(db, config, text: str):
    async with db.session() as session:
        return await listing_service.update_advertisement(
            session, config, guild_id=GID, text=text, actor_id=OWNER, now=NOW + timedelta(minutes=5)
        )


async def stored(db):
    async with db.session() as session:
        return await repository.get_listing(session, GID)


async def test_ad_edit_of_approved_listing_waits_and_old_ad_stays_live(db, strict):
    await approved_listing(db, strict)
    edited = await edit_ad(db, strict, "**Completely different ad**")
    assert edited.awaiting_review
    listing = await stored(db)
    assert listing.status == ListingStatus.ACTIVE  # still visible
    assert listing.advertisement_text == "**Original ad**"  # old approved content stays live
    assert listing.pending_changes == {"advertisement_text": "**Completely different ad**"}


async def test_approving_the_edit_makes_new_content_live(db, strict):
    await approved_listing(db, strict)
    await edit_ad(db, strict, "New text")
    revision = (await stored(db)).review_revision
    async with db.session() as session:
        _listing, kind = await listing_service.review_listing(
            session, guild_id=GID, approve=True, moderator_id=STAFF, now=NOW, revision=revision
        )
    assert kind == "edit"
    listing = await stored(db)
    assert listing.advertisement_text == "New text" and listing.pending_changes is None


async def test_rejecting_the_edit_keeps_the_old_ad(db, strict):
    await approved_listing(db, strict)
    await edit_ad(db, strict, "Spam spam spam")
    revision = (await stored(db)).review_revision
    async with db.session() as session:
        await listing_service.review_listing(session, guild_id=GID, approve=False, moderator_id=STAFF, now=NOW, revision=revision)
    listing = await stored(db)
    assert listing.advertisement_text == "**Original ad**"
    assert listing.status == ListingStatus.ACTIVE and listing.pending_changes is None


async def test_editing_again_outdates_the_previous_review(db, strict):
    await approved_listing(db, strict)
    await edit_ad(db, strict, "Version A")
    old_revision = (await stored(db)).review_revision
    await edit_ad(db, strict, "Version B — changed after staff saw A")
    with pytest.raises(Conflict, match="outdated"):
        async with db.session() as session:
            await listing_service.review_listing(
                session, guild_id=GID, approve=True, moderator_id=STAFF, now=NOW, revision=old_revision
            )
    assert (await stored(db)).advertisement_text == "**Original ad**"  # stale approval changed nothing


async def test_pending_new_listing_edit_needs_the_new_review(db, strict):
    async with db.session() as session:
        listing = await make_listing(session, strict, GID)
        first = listing.review_revision
    await edit_ad(db, strict, "Changed before approval")
    with pytest.raises(Conflict, match="outdated"):
        async with db.session() as session:
            await listing_service.review_listing(session, guild_id=GID, approve=True, moderator_id=STAFF, now=NOW, revision=first)


async def test_info_edits_skip_review_by_default_but_can_require_it(db, strict):
    await approved_listing(db, strict)

    async def change_category(config):
        async with db.session() as session:
            return await listing_service.update_info(
                session, config, guild_id=GID, categories=["Anime"], accepting_partnerships=True, minimum_members=0,
                contact_ids=[OWNER], invite_url="https://discord.gg/abc123", actor_id=OWNER, now=NOW,
            )

    await change_category(strict)
    assert (await stored(db)).categories == ["Anime"]  # reapprove_info_edits is off by default

    stricter = replace(strict, listings=replace(strict.listings, reapprove_info_edits=True))
    async with db.session() as session:
        await listing_service.update_info(
            session, stricter, guild_id=GID, categories=["Social"], accepting_partnerships=True, minimum_members=0,
            contact_ids=[OWNER], invite_url="https://discord.gg/abc123", actor_id=OWNER, now=NOW,
        )
    listing = await stored(db)
    assert listing.categories == ["Anime"] and listing.pending_changes == {"category": "Social"}


async def test_reapproval_can_be_switched_off(db, strict):
    relaxed = replace(strict, listings=replace(strict.listings, reapprove_ad_edits=False))
    await approved_listing(db, relaxed)
    await edit_ad(db, relaxed, "Straight to live")
    assert (await stored(db)).advertisement_text == "Straight to live"


async def test_without_approval_edits_apply_immediately(db, config):
    async with db.session() as session:
        await make_listing(session, config, GID)
    await edit_ad(db, config, "Immediate")
    listing = await stored(db)
    assert listing.advertisement_text == "Immediate" and not listing.awaiting_review


async def test_takedown_clears_pending_edits(db, strict):
    await approved_listing(db, strict)
    await edit_ad(db, strict, "Pending")
    async with db.session() as session:
        await moderation.suspend_listing(session, guild_id=GID, reason=None, moderator_id=STAFF)
    listing = await stored(db)
    assert listing.pending_changes is None
    with pytest.raises(Conflict):
        async with db.session() as session:
            await listing_service.review_listing(session, guild_id=GID, approve=True, moderator_id=STAFF, now=NOW)


async def test_old_review_buttons_without_revision_still_work_for_first_review(db, strict):
    async with db.session() as session:
        await make_listing(session, strict, GID)
    async with db.session() as session:
        listing, kind = await listing_service.review_listing(session, guild_id=GID, approve=True, moderator_id=STAFF, now=NOW)
    assert kind == "new" and listing.status == ListingStatus.ACTIVE

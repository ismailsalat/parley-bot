"""Listings: one listing per Guild ID, editing in place, refresh cooldown."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from bot.database import repository
from bot.database.models import Listing, ListingStatus
from bot.services import listings as listing_service
from bot.services.errors import Conflict, CooldownActive, NotFound, ValidationError
from tests.factories import NOW, OWNER, guild, listing_input, make_listing

GID = 111111111111111111


async def count_listings(session) -> int:
    return int(await session.scalar(select(func.count(Listing.id))))


# ---------------------------------------------------------------- duplicate prevention


async def test_create_listing_stores_guild_and_owner_contact(db, config):
    async with db.session() as session:
        listing = await make_listing(session, config, GID)
    async with db.session() as session:
        stored = await repository.get_listing(session, GID)
        assert stored is not None and stored.status == ListingStatus.ACTIVE
        assert stored.categories == ["Gaming"]
        assert (await repository.get_guild(session, GID)).connected_by == OWNER
        # the person who set it up is a contact by default
        assert await repository.get_contact_ids(session, GID) == [OWNER]
    assert listing.refreshed_at == NOW


async def test_same_guild_cannot_be_listed_twice(db, config):
    async with db.session() as session:
        await make_listing(session, config, GID)
    with pytest.raises(Conflict, match="This server is already listed."):
        async with db.session() as session:
            await make_listing(session, config, GID, invite_url="https://discord.gg/different-invite")
    async with db.session() as session:
        assert await count_listings(session) == 1


async def test_database_constraint_rejects_second_listing_row(db, config):
    """Even if application checks were bypassed, the unique constraint holds."""
    async with db.session() as session:
        await make_listing(session, config, GID)
    with pytest.raises(IntegrityError):
        async with db.session() as session:
            session.add(Listing(guild_id=GID, advertisement_text="dup", category="Gaming", invite_url="https://discord.gg/x"))
            await session.flush()


async def test_relisting_after_removal_reuses_the_same_row(db, config):
    async with db.session() as session:
        first = await make_listing(session, config, GID)
    async with db.session() as session:
        await listing_service.remove_listing(session, guild_id=GID, actor_id=OWNER, now=NOW)
    async with db.session() as session:
        again = await make_listing(session, config, GID, now=NOW + timedelta(days=1))
        assert again.id == first.id
        assert again.status == ListingStatus.ACTIVE
        assert await count_listings(session) == 1


async def test_approval_mode_creates_pending_listing(db, config):
    config = replace(config, listings=replace(config.listings, approval_required=True))
    async with db.session() as session:
        listing = await make_listing(session, config, GID)
    assert listing.status == ListingStatus.PENDING


# ---------------------------------------------------------------- validation


async def test_advertisement_formatting_is_preserved_exactly(db, config):
    ad = (
        "## <:emoji:123> __ PAID GIVEAWAY __\n\n"
        "## `10£` gw package\n"
        "- **Nitro giveaway**\n"
        "- @here\n"
        "- 14 days    with   spacing ✨ 日本語"
    )
    async with db.session() as session:
        await make_listing(session, config, GID, advertisement_text=f"\n\n{ad}\n  ")
    async with db.session() as session:
        assert (await repository.get_listing(session, GID)).advertisement_text == ad


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"advertisement_text": "   "}, "empty"),
        ({"advertisement_text": "x" * 1801}, "limit is 1800"),
        ({"categories": ["NotACategory"]}, "choose a category"),
        ({"invite_url": None}, "invite link is required"),
        ({"contact_ids": [1, 2, 3, 4]}, "up to 3"),
    ],
)
async def test_invalid_listing_input_is_rejected(db, config, fields, message):
    with pytest.raises(ValidationError, match=message):
        async with db.session() as session:
            await make_listing(session, config, GID, **fields)


def test_invite_parsing_accepts_common_formats():
    for raw in ("https://discord.gg/abc123", "discord.gg/abc123", "https://discord.com/invite/abc123/"):
        assert listing_service.parse_invite_code(raw) == "abc123"
    with pytest.raises(ValidationError):
        listing_service.parse_invite_code("https://evil.example/abc")


# ---------------------------------------------------------------- editing


async def test_editing_changes_the_same_listing(db, config):
    async with db.session() as session:
        original = await make_listing(session, config, GID)
    async with db.session() as session:
        await listing_service.update_advertisement(
            session, config, guild_id=GID, text="**New ad**", actor_id=OWNER, now=NOW + timedelta(minutes=5)
        )
        await listing_service.update_info(
            session,
            config,
            guild_id=GID,
            categories=["Anime"],
            accepting_partnerships=False,
            minimum_members=250,
            contact_ids=[OWNER, 2000],
            invite_url="https://discord.gg/newcode",
            actor_id=OWNER,
            now=NOW + timedelta(minutes=5),
        )
    async with db.session() as session:
        stored = await repository.get_listing(session, GID)
        assert stored.id == original.id
        assert stored.advertisement_text == "**New ad**"
        assert stored.categories == ["Anime"]
        assert stored.accepting_partnerships is False
        assert stored.minimum_members == 0  # minimum only applies while accepting
        assert stored.invite_url == "https://discord.gg/newcode"
        assert sorted(await repository.get_contact_ids(session, GID)) == [OWNER, 2000]
        assert await count_listings(session) == 1


async def test_editing_a_removed_listing_fails(db, config):
    async with db.session() as session:
        await make_listing(session, config, GID)
        await listing_service.remove_listing(session, guild_id=GID, actor_id=OWNER, now=NOW)
    with pytest.raises(NotFound, match="This listing no longer exists."):
        async with db.session() as session:
            await listing_service.update_advertisement(session, config, guild_id=GID, text="x", actor_id=OWNER, now=NOW)


# ---------------------------------------------------------------- refresh cooldown


async def test_refresh_cooldown_message(db, config):
    async with db.session() as session:
        await make_listing(session, config, GID)
    with pytest.raises(CooldownActive) as caught:
        async with db.session() as session:
            await listing_service.claim_refresh(
                session, config, guild_id=GID, actor_id=OWNER, now=NOW + timedelta(minutes=23)
            )
    assert caught.value.user_message == "You can Relist again in 7 minutes."


async def test_refresh_allowed_after_cooldown_and_blocks_double_click(db, config):
    async with db.session() as session:
        await make_listing(session, config, GID)
    later = NOW + timedelta(minutes=config.listings.refresh_cooldown_minutes)
    async with db.session() as session:
        listing = await listing_service.claim_refresh(session, config, guild_id=GID, actor_id=OWNER, now=later)
        assert listing.refreshed_at == later
    with pytest.raises(CooldownActive):
        async with db.session() as session:
            await listing_service.claim_refresh(
                session, config, guild_id=GID, actor_id=OWNER, now=later + timedelta(seconds=1)
            )


async def test_refresh_cooldown_is_configurable(db, config):
    config = replace(config, listings=replace(config.listings, refresh_cooldown_minutes=5))
    async with db.session() as session:
        await make_listing(session, config, GID)
    async with db.session() as session:
        await listing_service.claim_refresh(session, config, guild_id=GID, actor_id=OWNER, now=NOW + timedelta(minutes=5))


async def test_expired_listing_comes_back_on_refresh(db, config):
    async with db.session() as session:
        await make_listing(session, config, GID)
    expiry = NOW + timedelta(days=config.listings.expiration_days, minutes=1)
    async with db.session() as session:
        expired = await listing_service.expire_listings(session, config, expiry)
        assert [row.guild_id for row in expired] == [GID]
    async with db.session() as session:
        listing = await listing_service.claim_refresh(session, config, guild_id=GID, actor_id=OWNER, now=expiry)
        assert listing.status == ListingStatus.ACTIVE


async def test_suspended_listing_cannot_refresh(db, config):
    from bot.services import moderation

    async with db.session() as session:
        await make_listing(session, config, GID)
        await moderation.suspend_listing(session, guild_id=GID, reason=None, moderator_id=1)
    with pytest.raises(ValidationError, match="suspended"):
        async with db.session() as session:
            await listing_service.claim_refresh(session, config, guild_id=GID, actor_id=OWNER, now=NOW + timedelta(days=1))


def test_guild_factory_matches_service_type():
    assert guild(1).guild_id == 1 and listing_input().categories == ["Gaming"]

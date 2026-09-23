from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from bot.database import repository
from bot.services import cooldowns, testmode
from bot.services.errors import ValidationError
from tests.factories import NOW, make_listing
from tests.fakes import FakeBot, OWNER_ID


async def test_reset_test_cooldowns_only_resets_test_state(db, config):
    async with db.session() as session:
        test_listing = await make_listing(session, config, 101, now=NOW)
        test_listing.is_test = True
        test_listing.refreshed_at = NOW
        test_listing.last_ad_edit_at = NOW

        live_listing = await make_listing(session, config, 202, now=NOW)
        live_listing.refreshed_at = NOW
        live_listing.last_ad_edit_at = NOW

        await cooldowns.start(session, cooldowns.SCOPE_LOOKING_POST, 101, NOW, timedelta(hours=1))
        await cooldowns.start(session, cooldowns.SCOPE_LOOKING_POST, 202, NOW, timedelta(hours=1))
        await cooldowns.start(session, cooldowns.SCOPE_REQUEST_USER, OWNER_ID, NOW, timedelta(hours=1))
        await cooldowns.start(session, cooldowns.SCOPE_DECLINED_PAIR, "101:202", NOW, timedelta(hours=1))

    async with db.session() as session:
        listings, removed = await testmode.reset_test_cooldowns(session, actor_id=OWNER_ID)
        assert listings == 1
        assert removed >= 3

    async with db.session() as session:
        test_listing = await repository.get_listing(session, 101)
        live_listing = await repository.get_listing(session, 202)
        assert test_listing.refreshed_at is None and test_listing.last_ad_edit_at is None
        assert live_listing.refreshed_at == NOW and live_listing.last_ad_edit_at == NOW
        assert await cooldowns.remaining(session, cooldowns.SCOPE_LOOKING_POST, 101, NOW) is None
        assert await cooldowns.remaining(session, cooldowns.SCOPE_REQUEST_USER, OWNER_ID, NOW) is None
        assert await cooldowns.remaining(session, cooldowns.SCOPE_DECLINED_PAIR, "101:202", NOW) is None
        assert await cooldowns.remaining(session, cooldowns.SCOPE_LOOKING_POST, 202, NOW) is not None


async def test_clear_test_listings_hard_resets_only_test_listings(db, config):
    bot = FakeBot(db, mode="test")
    deleted: list[tuple[int, int]] = []

    async def delete_listing_message(channel_id, message_id):
        deleted.append((channel_id, message_id))

    bot.panels = SimpleNamespace(delete_listing_message=delete_listing_message)

    async with db.session() as session:
        test_listing = await make_listing(session, config, 101, now=NOW)
        test_listing.is_test = True
        test_listing.channel_id = 50
        test_listing.message_id = 51
        test_listing.controls_message_id = 52
        test_listing.review_channel_id = 60
        test_listing.review_message_id = 61
        await make_listing(session, config, 202, now=NOW)

    assert await testmode.clear_test_listings(bot, actor_id=OWNER_ID) == 1
    assert set(deleted) == {(50, 51), (50, 52), (60, 61)}

    async with db.session() as session:
        assert await repository.get_listing(session, 101) is None
        assert await repository.get_listing(session, 202) is not None


async def test_clear_test_listings_is_blocked_in_live_mode(db):
    bot = FakeBot(db, mode="live")
    with pytest.raises(ValidationError, match="TEST mode"):
        await testmode.clear_test_listings(bot, actor_id=OWNER_ID)

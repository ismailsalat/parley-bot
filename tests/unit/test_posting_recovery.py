"""Regressions for Camelot posting, ownership, saved timers, and delivery failures."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from sqlalchemy import delete, select

from bot.config.runtime import apply_overrides, default_config
from bot.database import repository
from bot.database.models import AuditLog, Cooldown, ListingStatus
from bot.services import configuration, listings, quick_post, testmode
from bot.services.errors import Conflict, CooldownActive, ParleyError, ValidationError
from bot.views import quick_post as quick_view
from bot.views.admin.settings import SimplePostingPage
from bot.views.partnership import listing_message_kwargs
from tests.fakes import FakeBot, FakeInteraction
from tests.unit.test_quick_post import NOW, guild, submit
from tests.unit.test_panels import FakeBot as PanelBot, LISTINGS, service_for

CAMELOT_AD = """_ _
# _ _                      **𝒞**𝑎𝑚𝑒𝑙𝑜𝑡
_ _                         social・chill・late nights
_ _              ◟   enter the kingdom   [◞](https://discord.gg/vMZCgZZ4T)
_ _                      new faces・your next home
_ _                         a seat at our round table
_ _
https://discord.gg/vMZCgZZ4T
_ _"""


@pytest.mark.parametrize('key', ['refresh_cooldown_minutes', 'quick_post_cooldown_minutes'])
@pytest.mark.parametrize('minutes', [0, 15, 30, 1440])
async def test_saved_free_setting_is_enforced_and_survives_reload(db, config, key, minutes):
    async with db.session() as session:
        await configuration.save(session, {f'listings.{key}': minutes}, actor_id=1)
    async with db.session() as session:
        loaded, errors = apply_overrides(default_config(), await repository.runtime_overrides(session))
        assert not errors
        assert loaded.listings.quick_post_cooldown_minutes == minutes
        assert loaded.listings.refresh_cooldown_minutes == minutes
        await submit(session, loaded)
    if minutes:
        async with db.session() as session:
            with pytest.raises(CooldownActive):
                await submit(session, loaded, at=NOW + timedelta(minutes=minutes, seconds=-1))
    async with db.session() as session:
        _, result = await submit(session, loaded, at=NOW + timedelta(minutes=minutes))
        assert result == 'reposted'


async def test_old_saved_setting_and_new_setting_do_not_compete(db):
    async with db.session() as session:
        await repository.set_runtime_setting(session, 'listings.refresh_cooldown_minutes', 15)
        loaded, _ = apply_overrides(default_config(), await repository.runtime_overrides(session))
        assert loaded.listings.quick_post_cooldown_minutes == 15
        await configuration.save(session, {'listings.quick_post_cooldown_minutes': 90}, actor_id=1)
        latest = await configuration.save(session, {'listings.refresh_cooldown_minutes': 5}, actor_id=1)
        assert latest.listings.quick_post_cooldown_minutes == latest.listings.refresh_cooldown_minutes == 5


@pytest.mark.parametrize('bad', [-1, 10081, True, 1.5, 'soon'])
async def test_bad_cooldown_does_not_partially_save(db, bad):
    async with db.session() as session:
        with pytest.raises(ValidationError):
            await configuration.save(session, {'listings.quick_post_cooldown_minutes': bad}, actor_id=1)
        assert not await repository.runtime_overrides(session)


async def test_twenty_hour_wait_uses_new_setting_for_existing_ad(db, config):
    async with db.session() as session:
        row, _ = await submit(session, config)
        old_text = row.advertisement_text
    later = NOW + timedelta(hours=3, minutes=49)
    async with db.session() as session:
        with pytest.raises(CooldownActive) as caught:
            await submit(session, config, at=later)
        assert '20 hours 11 minutes' in str(caught.value)
        shorter = await configuration.save(session, {'listings.refresh_cooldown_minutes': 30}, actor_id=1)
        row, result = await submit(session, shorter, at=later)
        assert result == 'reposted' and row.advertisement_text == old_text


@pytest.mark.parametrize('legacy', [False, True])
async def test_switch_timer_recalculates_after_setting_change(db, config, legacy):
    async with db.session() as session:
        await submit(session, config)
        await quick_post.delete_quick_listing(session, config, actor_id=2002, guild_id=1001,
                                               now=NOW + timedelta(minutes=5))
        if legacy:
            event = await session.scalar(select(AuditLog).where(AuditLog.action == 'listing.quick_deleted'))
            event.details = {}
    async with db.session() as session:
        shorter = await configuration.save(session, {'listings.quick_post_cooldown_minutes': 30}, actor_id=1)
        # Expired cooldown row cleanup must not discard the durable cycle start.
        await session.execute(delete(Cooldown))
        assert await quick_post.deleted_cooldown_remaining(session, shorter, 2002, NOW + timedelta(minutes=10)) == timedelta(minutes=20)
        _, result = await submit(session, shorter, gid=1002, at=NOW + timedelta(minutes=31))
        assert result == 'published'


async def test_camelot_markdown_and_repeated_same_invite_are_preserved(db, config):
    assert quick_post.advertisement_invite_codes(CAMELOT_AD) == ['vMZCgZZ4T']
    async with db.session() as session:
        row, result = await quick_post.create_quick_listing(
            session, config, info=guild(1001), actor_id=2002,
            invite_url='https://discord.gg/vMZCgZZ4T', description='', category='Social',
            raw_ad=CAMELOT_AD, now=NOW,
        )
        assert result == 'published' and row.advertisement_text == CAMELOT_AD
        payload = listing_message_kwargs(FakeBot(db), row)
        assert payload['content'].endswith(CAMELOT_AD)
        assert 'Submitted by <@2002>' in payload['content']
        assert payload['allowed_mentions'].to_dict()['parse'] == []


async def test_ownership_error_identifies_submitter_without_transferring_ad(db, config):
    async with db.session() as session:
        original, _ = await submit(session, config)
        text = original.advertisement_text
    async with db.session() as session:
        with pytest.raises(Conflict, match='<@2002>'):
            await submit(session, config, user=3003, at=NOW + timedelta(days=2))
        row = await repository.get_listing(session, 1001)
        assert row.advertisement_text == text and row.quick_submitted_by == 2002
        assert '`2002`' in await quick_post.listing_attribution(session, row)
        row.quick_submitted_by = None
        assert '<@2002>' in await quick_post.listing_attribution(session, row)  # historical audit fallback


async def test_unknown_legacy_author_is_not_guessed(db, config):
    from tests.factories import make_listing
    async with db.session() as session:
        row = await make_listing(session, config, 999)
        await session.execute(delete(AuditLog))
        assert 'Not recorded' in await quick_post.listing_attribution(session, row)


async def test_staff_timer_reset_keeps_ad_and_other_users_timer(db, config):
    bot = FakeBot(db)
    async with db.session() as session:
        mine, _ = await submit(session, config, user=1)
        mine.message_id = 444
        saved = mine.advertisement_text
        await submit(session, config, gid=1002, user=4)
    await testmode.reset_my_quick_cooldown(bot, actor_id=1)
    async with db.session() as session:
        row = await repository.get_listing(session, 1001)
        other = await repository.get_listing(session, 1002)
        assert row.advertisement_text == saved and row.message_id == 444
        assert row.status == ListingStatus.ACTIVE and row.quick_submitted_by == 1
        assert row.refreshed_at is None
        assert other.refreshed_at == NOW
        _, result = await submit(session, config, user=1, at=NOW + timedelta(minutes=1))
        assert result == 'reposted'


async def test_member_cannot_reset_timer_and_staff_reset_cannot_unsuspend(db, config):
    bot = FakeBot(db)
    async with db.session() as session:
        mine, _ = await submit(session, config, user=4)
        staff, _ = await submit(session, config, gid=1002, user=1)
        staff.status = ListingStatus.SUSPENDED
    with pytest.raises(ParleyError):
        await testmode.reset_my_quick_cooldown(bot, actor_id=4)
    await testmode.reset_my_quick_cooldown(bot, actor_id=1)
    async with db.session() as session:
        assert (await repository.get_listing(session, 1001)).refreshed_at == mine.refreshed_at
        with pytest.raises(Conflict, match='suspended'):
            await submit(session, config, gid=1002, user=1)


async def test_staff_can_reset_own_deleted_slot_and_restore_it(db, config):
    bot = FakeBot(db)
    async with db.session() as session:
        await submit(session, config, user=1)
        await quick_post.delete_quick_listing(session, config, actor_id=1, guild_id=1001,
                                               now=NOW + timedelta(minutes=1))
    await testmode.reset_my_quick_cooldown(bot, actor_id=1)
    async with db.session() as session:
        assert await quick_post.deleted_cooldown_remaining(session, config, 1, NOW) is None
        _, result = await submit(session, config, user=1, at=NOW + timedelta(minutes=2))
        assert result == 'published'


async def test_settings_form_saves_both_timers(db):
    bot = FakeBot(db)
    async def reload():
        async with db.session() as session:
            bot.runtime, _ = apply_overrides(bot.runtime, await repository.runtime_overrides(session))
    bot.settings_changed = reload
    page = SimplePostingPage(bot, 1)
    interaction = FakeInteraction(bot, 1)
    interaction.response.send_modal = AsyncMock()
    await page._edit_cooldowns(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    await modal._callback(FakeInteraction(bot, 1), {
        'listings.quick_post_cooldown_minutes': '15',
        'listings.connected_refresh_cooldown_minutes': '7',
    })
    assert bot.runtime.listings.quick_post_cooldown_minutes == 15
    assert bot.runtime.listings.connected_refresh_cooldown_minutes == 7
    assert '**Free repost:** 15 minutes' in page.content()
    assert {c.label for c in page.children} >= {'Edit Repost Cooldowns', 'Reset My Repost Timer'}


async def test_cooldown_menu_keeps_refresh_and_edit_available(db, config, monkeypatch):
    bot = FakeBot(db)
    monkeypatch.setattr(quick_view, 'utcnow', lambda: NOW + timedelta(minutes=1))
    async with db.session() as session:
        row, _ = await submit(session, config, user=4)
        row.message_id = 123
    interaction = FakeInteraction(bot, 4)
    await quick_view.show_quick_start(interaction)
    sent = interaction.response.sent[-1]
    controls = {item.label: item for item in sent['view'].children}
    assert controls['Repost Existing Ad'].disabled
    assert not controls['Check Cooldown'].disabled and not controls['Edit Ad'].disabled
    assert '<@4>' in sent['content'] and '**Free repost setting:** 1440 minutes' in sent['content']


async def test_failed_repost_restores_previous_timer_and_leaves_controls(db, config, monkeypatch):
    bot = FakeBot(db)
    monkeypatch.setattr(quick_view, 'utcnow', lambda: NOW + timedelta(days=2))
    bot.fetch_invite = AsyncMock(return_value=SimpleNamespace(guild=SimpleNamespace(id=1001)))
    bot.panels.publish_listing = AsyncMock(side_effect=discord.HTTPException(
        SimpleNamespace(status=503, reason='Offline'), 'Unavailable'))
    async with db.session() as session:
        row, _ = await submit(session, config, user=4)
        row.message_id = 321
    interaction = FakeInteraction(bot, 4)
    await quick_view.QuickStartView(bot, 4, existing=True)._repost(interaction)
    async with db.session() as session:
        row = await repository.get_listing(session, 1001)
        assert row.refreshed_at == NOW and row.message_id == 321
    sent = interaction.response.sent[-1]
    assert 'not charged' in sent['content'] and sent['view'] is not None


async def test_retry_saved_ad_does_not_claim_another_cycle(db, config, monkeypatch):
    bot = FakeBot(db)
    monkeypatch.setattr(quick_view, 'utcnow', lambda: NOW + timedelta(minutes=1))
    async with db.session() as session:
        await submit(session, config, user=4)
    bot.panels.publish_listing = AsyncMock(return_value=None)
    interaction = FakeInteraction(bot, 4)
    await quick_view.show_quick_start(interaction)
    controls = {item.label: item for item in interaction.response.sent[-1]['view'].children}
    assert not controls['Retry Saved Ad'].disabled
    await controls['Retry Saved Ad'].callback(FakeInteraction(bot, 4))
    bot.panels.publish_listing.assert_awaited_once_with(1001, only_if_missing=True)
    async with db.session() as session:
        assert (await repository.get_listing(session, 1001)).refreshed_at == NOW


async def test_stale_failure_cannot_reset_a_newer_post(db, config):
    async with db.session() as session:
        row, _ = await submit(session, config)
        row.refreshed_at = NOW + timedelta(days=2)
        row.message_id = 999
    async with db.session() as session:
        assert not await listings.release_failed_refresh(
            session, guild_id=1001, claimed_at=NOW + timedelta(days=1), previous_at=NOW,
            previous_message_id=111, previous_status=ListingStatus.ACTIVE,
        )
        assert (await repository.get_listing(session, 1001)).message_id == 999


async def test_parallel_saved_ad_retries_publish_once(db, config):
    import asyncio
    bot = PanelBot(db)
    service = service_for(bot)
    async with db.session() as session:
        await submit(session, config)
    first, second = await asyncio.gather(
        service.publish_listing(1001, only_if_missing=True),
        service.publish_listing(1001, only_if_missing=True),
    )
    assert first.id == second.id
    ad_messages = [m for m in bot.channels[LISTINGS].messages.values() if 'friendly game server' in (m.content or '')]
    assert len(ad_messages) == 1
    async with db.session() as session:
        assert (await repository.get_listing(session, 1001)).message_id == first.id


async def test_auxiliary_panel_failure_does_not_report_ad_failure(db, config):
    bot = PanelBot(db)
    service = service_for(bot)
    service._repost_panel = AsyncMock(side_effect=RuntimeError('panel unavailable'))
    async with db.session() as session:
        await submit(session, config)
    delivered = await service.publish_listing(1001)
    assert delivered is not None
    async with db.session() as session:
        assert (await repository.get_listing(session, 1001)).message_id == delivered.id


async def test_public_prefix_does_not_overflow_discord_limit(db, config):
    async with db.session() as session:
        row, _ = await submit(session, config)
        row.advertisement_text = 'x' * 2000  # legacy maximum length
        row.is_test = True
        assert len(listing_message_kwargs(FakeBot(db), row)['content']) == 2000


async def test_edit_accepts_same_server_vanity_and_keeps_management_controls(db, config, monkeypatch):
    bot = FakeBot(db)
    monkeypatch.setattr(quick_view, 'utcnow', lambda: NOW + timedelta(minutes=1))
    bot.fetch_invite = AsyncMock(return_value=SimpleNamespace(
        code='vMZCgZZ4T', guild=SimpleNamespace(id=1001, name='Camelot', icon=None),
        approximate_member_count=100,
    ))
    bot.panels.update_listing_message = AsyncMock()
    bot.panels.review_channel = lambda: None
    async with db.session() as session:
        await submit(session, config, user=4)
    modal = quick_view.QuickPostModal(bot, 'Social', editing=True)
    modal.description._value = CAMELOT_AD
    interaction = FakeInteraction(bot, 4)
    await modal.on_submit(interaction)
    confirmation = interaction.response.sent[-1]['view']
    assert isinstance(confirmation, quick_view.QuickConfirmView)
    assert confirmation.draft.invite_url == 'https://discord.gg/testInvite'
    await confirmation._submit(FakeInteraction(bot, 4))
    async with db.session() as session:
        row = await repository.get_listing(session, 1001)
        assert row.advertisement_text == CAMELOT_AD
        assert row.refreshed_at == NOW  # editing is not a repost
    bot.panels.update_listing_message.assert_awaited_once_with(1001)

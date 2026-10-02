"""Crash recovery: private draft channels are disposable, listings are durable."""
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from bot.database.models import TemporaryAdSession
from bot.utils.helpers import utcnow
from bot.views import temp_ads


def make_bot(db, channels=()):
    return SimpleNamespace(
        db=db,
        runtime=SimpleNamespace(hub=SimpleNamespace(main_guild_id=999)),
        get_guild=lambda _id: SimpleNamespace(text_channels=list(channels)),
    )


async def test_maintenance_only_cleans_expired_drafts(db, monkeypatch):
    now = utcnow()
    async with db.session() as session:
        session.add_all([
            TemporaryAdSession(channel_id=8001, hub_guild_id=999, advertised_guild_id=101,
                               user_id=701, created_at=now-timedelta(minutes=12),
                               expires_at=now-timedelta(minutes=2)),
            TemporaryAdSession(channel_id=8002, hub_guild_id=999, advertised_guild_id=102,
                               user_id=702, created_at=now,
                               expires_at=now+timedelta(minutes=5)),
        ])
    calls = []
    async def delete(_bot, cid, _reason):
        calls.append(cid)
        return True
    monkeypatch.setattr(temp_ads, "_delete_channel", delete)
    temp_ads._ORPHAN_RETRY_IDS.clear()
    removed = await temp_ads.cleanup(make_bot(db))
    assert removed == 1 and calls == [8001]
    async with db.session() as session:
        rows = list(await session.scalars(select(TemporaryAdSession)))
        assert [r.channel_id for r in rows] == [8002]


async def test_restart_deletes_unfinished_drafts_and_orphans(db, monkeypatch):
    now = utcnow()
    async with db.session() as session:
        session.add(TemporaryAdSession(channel_id=8101, hub_guild_id=999, advertised_guild_id=101,
                                      user_id=801, created_at=now, expires_at=now+timedelta(minutes=9)))
    calls = []
    async def delete(_bot, cid, _reason):
        calls.append(cid)
        return True
    monkeypatch.setattr(temp_ads, "_delete_channel", delete)
    temp_ads._ORPHAN_RETRY_IDS.clear()
    orphan = SimpleNamespace(id=8102, name="parley-temp-ad-802")
    assert await temp_ads.cleanup(make_bot(db, [orphan]), startup=True) == 2
    assert set(calls) == {8101, 8102}
    async with db.session() as session:
        assert (await session.scalar(select(TemporaryAdSession))) is None
    temp_ads._ORPHAN_RETRY_IDS.clear()


async def test_failed_delete_keeps_recovery_record_for_retry(db, monkeypatch):
    now = utcnow()
    async with db.session() as session:
        session.add(TemporaryAdSession(channel_id=8201, hub_guild_id=999, advertised_guild_id=101,
                                      user_id=901, created_at=now-timedelta(minutes=15),
                                      expires_at=now-timedelta(minutes=5)))
    attempts = 0
    async def flaky(_bot, cid, _reason):
        nonlocal attempts
        attempts += 1
        return attempts >= 2
    monkeypatch.setattr(temp_ads, "_delete_channel", flaky)
    temp_ads._ORPHAN_RETRY_IDS.clear()
    bot = make_bot(db)
    assert await temp_ads.cleanup(bot) == 0
    async with db.session() as session:
        assert await session.get(TemporaryAdSession, 8201) is not None
    assert await temp_ads.cleanup(bot) == 1
    async with db.session() as session:
        assert await session.get(TemporaryAdSession, 8201) is None

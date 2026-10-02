"""Regression checks for the four production gaps found in the Parley 4 audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

from bot.database.models import PendingMessageDeletion
from bot.services import listings, quick_post
from bot.services.errors import Conflict
from tests.factories import guild
from tests.unit.test_panels import LISTINGS, service_for

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_discord_attachment_images_do_not_trigger_external_review(config):
    image = "https://cdn.discordapp.com/attachments/123456789/987654321/castle.png?ex=abc123"
    raw = f"# Camelot\n{image}\nhttps://discord.gg/camelot"
    text, review = quick_post.prepare_quick_ad(
        config, info=guild(4422), invite_url="https://discord.gg/camelot",
        category="Gaming", raw_ad=raw,
    )
    assert text == raw
    assert review is False
    assert listings.is_discord_attachment_image(image)
    assert listings.is_discord_attachment_image(
        "https://media.discordapp.net/attachments/123/456/camelot.webp?width=200"
    )


@pytest.mark.parametrize("image", [
    "https://cdn.discordapp.com.evil.example/attachments/123/456/x.png",
    "https://cdn.discordapp.com/not-attachments/123/456/x.png",
    "https://cdn.discordapp.com/attachments/123/456/payload.exe",
    "http://cdn.discordapp.com/attachments/123/456/x.png",
    "https://cdn.discordapp.com@evil.example/attachments/123/456/x.png",
])
def test_discord_media_lookalikes_not_whitelisted(config, image):
    assert not listings.is_discord_attachment_image(image)
    _, review = quick_post.prepare_quick_ad(
        config, info=guild(4422), invite_url="https://discord.gg/camelot",
        category="Gaming", raw_ad=f"Camelot\n{image}\nhttps://discord.gg/camelot",
    )
    assert review is True


async def test_free_invite_can_be_replaced_but_not_claim_another_guild(db, config):
    async with db.session() as session:
        listing, outcome = await quick_post.create_quick_listing(
            session, config, info=guild(88991), actor_id=99881,
            invite_url="https://discord.gg/oldcastle", category="Gaming",
            description="A friendly gaming server", now=NOW,
        )
        previous_refresh = listing.refreshed_at
        assert "oldcastle" in listing.advertisement_text
    async with db.session() as session:
        updated = await quick_post.update_quick_invite(
            session, config, actor_id=99881, guild_id=88991, verified_guild_id=88991,
            new_invite_url="https://discord.gg/camelot", now=NOW + timedelta(minutes=3),
        )
        assert updated.invite_url == "https://discord.gg/camelot"
        assert "discord.gg/oldcastle" not in updated.advertisement_text
        assert "discord.gg/camelot" in updated.advertisement_text
        assert updated.refreshed_at == previous_refresh
        assert updated.last_ad_edit_at is None
    async with db.session() as session:
        with pytest.raises(Conflict, match="different Discord server"):
            await quick_post.update_quick_invite(
                session, config, actor_id=99881, guild_id=88991, verified_guild_id=123456,
                new_invite_url="https://discord.gg/other", now=NOW,
            )
    async with db.session() as session:
        with pytest.raises(Conflict, match="submitter"):
            await quick_post.update_quick_invite(
                session, config, actor_id=33999, guild_id=88991, verified_guild_id=88991,
                new_invite_url="https://discord.gg/other", now=NOW,
            )


async def test_old_post_failed_deletion_survives_relist_and_is_retried(db, config):
    from tests.unit.test_panels import FakeBot

    bot = FakeBot(db)
    service = service_for(bot)
    channel = bot.channels[LISTINGS]
    async with db.session() as session:
        from tests.factories import make_listing
        await make_listing(session, config, 88991)
    first = await service.publish_listing(88991)
    original = channel.get_partial_message

    class FailingDelete:
        async def delete(self):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "No permission")

    channel.get_partial_message = lambda mid: FailingDelete() if mid == first.id else original(mid)
    second = await service.publish_listing(88991)
    assert first.id != second.id
    async with db.session() as session:
        queued = await session.get(PendingMessageDeletion, (LISTINGS, first.id))
        assert queued is not None
    channel.get_partial_message = original
    removed, remaining = await service.retry_pending_message_deletions()
    assert (removed, remaining) == (1, 0)
    assert first.id not in channel.messages
    async with db.session() as session:
        assert await session.get(PendingMessageDeletion, (LISTINGS, first.id)) is None


async def test_missing_directory_post_is_actually_republished(db, config):
    from tests.unit.test_panels import FakeBot
    from tests.factories import make_listing

    bot = FakeBot(db)
    service = service_for(bot)
    async with db.session() as session:
        await make_listing(session, config, 88991)
    first = await service.publish_listing(88991)
    await first.delete()
    replacement = await service.publish_listing(88991, only_if_missing=True)
    assert replacement is not None and replacement.id != first.id


def test_staff_repair_uses_complete_history():
    import inspect
    from bot.views.admin.settings import ToolsMenu

    source = inspect.getsource(ToolsMenu._repair_panels)
    assert "cleanup_orphan_entry_panels(history_limit=None)" in source


def test_migration_defines_durable_cleanup_table():
    from bot.database.models import Base

    table = Base.metadata.tables["pending_message_deletions"]
    assert {"channel_id", "message_id", "guild_id", "reason"} <= set(table.c.keys())
    assert {col.name for col in table.primary_key} == {"channel_id", "message_id"}

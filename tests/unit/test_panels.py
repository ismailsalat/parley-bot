"""Panel state: the bottom panel always follows the newest listing and survives restarts.

The Discord channel is an in-memory stand-in that records sends/deletes, so
these tests verify Parley's ordering and persistence logic. Real Discord
delivery is covered by tests/integration.
"""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from dataclasses import replace

from bot.config.runtime import HubConfig, default_config
from bot.database import repository
from bot.services.panels import LISTINGS_PANEL, LOOKING_PANEL, PanelService
from tests.conftest import make_settings
from tests.factories import make_listing

MAIN, LISTINGS, LOOKING = 900000000000000001, 900000000000000002, 900000000000000003


def not_found() -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")


class FakeMessage:
    def __init__(self, channel: FakeChannel, message_id: int, content: str | None, view) -> None:
        self.channel, self.id, self.content, self.view = channel, message_id, content, view
        self.jump_url = f"https://discord.com/channels/{MAIN}/{channel.id}/{message_id}"

    async def edit(self, **kwargs) -> None:
        self.channel._require(self.id)
        self.content = kwargs.get("content", self.content)
        self.channel.log.append(("edit", self.id))

    async def delete(self) -> None:
        self.channel._require(self.id)
        del self.channel.messages[self.id]
        self.channel.log.append(("delete", self.id))


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.name = f"channel-{channel_id}"
        self.guild = SimpleNamespace(id=MAIN)
        self.messages: dict[int, FakeMessage] = {}
        self.log: list[tuple[str, int]] = []
        self._next = 1
        self.sent_kwargs: list[dict] = []

    def _require(self, message_id: int) -> None:
        if message_id not in self.messages:
            raise not_found()

    async def send(self, content=None, *, view=None, allowed_mentions=None, **kwargs) -> FakeMessage:
        assert allowed_mentions is not None and allowed_mentions.to_dict()["parse"] == []
        self.sent_kwargs.append({"content": content, **kwargs})
        message = FakeMessage(self, self._next, content, view)
        self._next += 1
        self.messages[message.id] = message
        self.log.append(("send", message.id))
        return message

    def get_partial_message(self, message_id: int) -> FakeMessage:
        return self.messages.get(message_id) or _Gone(self, message_id)

    async def fetch_message(self, message_id: int) -> FakeMessage:
        self._require(message_id)
        return self.messages[message_id]

    async def history(self, limit: int = 1):
        for message_id in sorted(self.messages, reverse=True)[:limit]:
            yield self.messages[message_id]

    def ids(self) -> list[int]:
        return sorted(self.messages)


class _Gone:
    def __init__(self, channel: FakeChannel, message_id: int) -> None:
        self.channel, self.id = channel, message_id

    async def delete(self) -> None:
        raise not_found()

    async def edit(self, **kwargs) -> None:
        raise not_found()


class FakeBot:
    def __init__(self, db) -> None:
        self.db = db
        # Channels come from the database-backed hub configuration (set with /setup).
        self.runtime = replace(
            default_config(),
            hub=HubConfig(main_guild_id=MAIN, listings_channel_id=LISTINGS, looking_channel_id=LOOKING),
        )
        self.settings = make_settings("sqlite://")
        self.channels = {LISTINGS: FakeChannel(LISTINGS), LOOKING: FakeChannel(LOOKING)}
        self.application_id = None

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    def get_guild(self, guild_id: int):
        return None


def service_for(bot: FakeBot) -> PanelService:
    service = PanelService(bot)  # type: ignore[arg-type]
    service.main_channel = lambda channel_id: bot.channels.get(channel_id)  # type: ignore[method-assign]
    return service


async def stored_panel(db, panel_type=LISTINGS_PANEL):
    async with db.session() as session:
        return await repository.get_panel(session, MAIN, panel_type)


@pytest.fixture
def bot(db):
    return FakeBot(db)


async def test_publishing_puts_panel_under_the_newest_listing(db, config, bot):
    service = service_for(bot)
    channel = bot.channels[LISTINGS]
    async with db.session() as session:
        await make_listing(session, config, 1)
        await make_listing(session, config, 2)

    first = await service.publish_listing(1)
    second = await service.publish_listing(2)

    panel = await stored_panel(db)
    # listing 1, listing 2, panel - and nothing else left in the channel
    assert channel.ids() == [first.id, second.id, panel.message_id]
    assert channel.messages[panel.message_id].content == bot.runtime.panels.listings_panel_text
    async with db.session() as session:
        assert (await repository.get_listing(session, 2)).message_id == second.id


async def test_ad_is_sent_as_plain_content(db, config, bot):
    service = service_for(bot)
    async with db.session() as session:
        await make_listing(session, config, 1, advertisement_text="## Hi @everyone")
    await service.publish_listing(1)
    sent = bot.channels[LISTINGS].sent_kwargs[0]
    assert sent["content"] == "## Hi @everyone"
    assert "embed" not in sent and "embeds" not in sent


async def test_refresh_moves_listing_without_duplicates(db, config, bot):
    service = service_for(bot)
    channel = bot.channels[LISTINGS]
    async with db.session() as session:
        await make_listing(session, config, 1)
        await make_listing(session, config, 2)
    old_first = await service.publish_listing(1)
    await service.publish_listing(2)

    moved = await service.publish_listing(1)  # normal bot-authored publish/reposition path

    panel = await stored_panel(db)
    assert old_first.id not in channel.messages  # the old copy was deleted
    assert channel.ids()[-2:] == [moved.id, panel.message_id]
    assert len(channel.ids()) == 3  # listing 2, listing 1, panel


async def test_panel_survives_restart_and_is_recreated_if_deleted(db, config, bot):
    async with db.session() as session:
        await make_listing(session, config, 1)
    await service_for(bot).publish_listing(1)
    panel_id = (await stored_panel(db)).message_id
    channel = bot.channels[LISTINGS]

    # "restart": a brand-new service instance only has the database
    restarted = service_for(bot)
    sends_before = len(channel.sent_kwargs)
    await restarted.restore_panels()
    assert (await stored_panel(db)).message_id == panel_id
    assert len([k for k in channel.sent_kwargs[sends_before:]]) == 0  # nothing reposted in #listings

    # someone deletes the panel while the bot is offline
    await channel.messages[panel_id].delete()
    await service_for(bot).restore_panels()
    new_panel = (await stored_panel(db)).message_id
    assert new_panel != panel_id and channel.ids()[-1] == new_panel


async def test_panel_moves_back_to_bottom_on_restore(db, config, bot):
    async with db.session() as session:
        await make_listing(session, config, 1)
    service = service_for(bot)
    await service.publish_listing(1)
    channel = bot.channels[LISTINGS]
    await channel.send("someone posted under the panel", allowed_mentions=discord.AllowedMentions.none())
    await service.restore_panels()
    assert channel.ids()[-1] == (await stored_panel(db)).message_id


async def test_edit_updates_in_place_or_reposts_if_deleted(db, config, bot):
    service = service_for(bot)
    channel = bot.channels[LISTINGS]
    async with db.session() as session:
        await make_listing(session, config, 1)
    first = await service.publish_listing(1)

    await service.update_listing_message(1)  # normal edit: same message, nothing new sent
    assert ("edit", first.id) in channel.log
    assert first.id in channel.messages

    await first.delete()  # a moderator deleted the ad by hand
    await service.update_listing_message(1)
    async with db.session() as session:
        stored = (await repository.get_listing(session, 1)).message_id
    assert stored != first.id and stored in channel.messages
    assert channel.ids()[-1] == (await stored_panel(db)).message_id


async def test_looking_panel_moves_only_when_asked(db, bot):
    service = service_for(bot)
    channel = bot.channels[LOOKING]
    await service.restore_panels()
    panel_id = (await stored_panel(db, LOOKING_PANEL)).message_id
    await channel.send("a reply in the thread of conversation", allowed_mentions=discord.AllowedMentions.none())
    await service.restore_panels()  # replies do not move it
    assert (await stored_panel(db, LOOKING_PANEL)).message_id == panel_id
    await service.move_looking_panel_now()  # a new top-level partnership post does
    assert channel.ids()[-1] == (await stored_panel(db, LOOKING_PANEL)).message_id != panel_id


async def test_inactive_or_unconfigured_listing_is_not_published(db, config, bot):
    service = service_for(bot)
    assert await service.publish_listing(123) is None
    bot.runtime = replace(bot.runtime, hub=HubConfig(main_guild_id=MAIN))  # no listings channel
    async with db.session() as session:
        await make_listing(session, config, 1)
    assert await service_for(bot).publish_listing(1) is None


async def test_panel_status_detects_deletion_and_repair_fixes_it(db, config, bot):
    service = service_for(bot)
    assert await service.panel_status(LISTINGS_PANEL) == "not_posted"
    await service.restore_panels()
    assert await service.panel_status(LISTINGS_PANEL) == "ok"
    panel_id = (await stored_panel(db)).message_id
    await bot.channels[LISTINGS].messages[panel_id].delete()
    assert await service.panel_status(LISTINGS_PANEL) == "missing"
    results = await service.restore_panels(force_edit=True)
    assert results[LISTINGS_PANEL] == "created"
    assert await service.panel_status(LISTINGS_PANEL) == "ok"


async def test_changed_panel_text_is_applied_live(db, bot):
    from dataclasses import replace as dc_replace

    service = service_for(bot)
    await service.restore_panels()
    bot.runtime = dc_replace(bot.runtime, panels=dc_replace(bot.runtime.panels, listings_panel_text="New text"))
    results = await service.restore_panels(force_edit=True)
    assert results[LISTINGS_PANEL] == "edited"
    panel_id = (await stored_panel(db)).message_id
    assert bot.channels[LISTINGS].messages[panel_id].content == "New text"


async def test_owner_posted_listing_keeps_controls_and_panel_underneath(db, config, bot):
    """Post It Myself: owner's message, Parley's buttons, then the moving panel."""
    service = service_for(bot)
    channel = bot.channels[LISTINGS]
    async with db.session() as session:
        await make_listing(session, config, 1)
    owner_message = await channel.send("## My own ad <:emoji:1>", allowed_mentions=discord.AllowedMentions.none())

    await service.adopt_self_post(1, owner_message)

    panel = await stored_panel(db)
    async with db.session() as session:
        listing = await repository.get_listing(session, 1)
    assert listing.message_id == owner_message.id and listing.self_posted is True
    assert listing.controls_message_id is not None
    # owner's message, Parley's controls, then the panel at the bottom
    assert channel.ids() == [owner_message.id, listing.controls_message_id, panel.message_id]


async def test_publishing_normally_clears_the_owner_posted_state(db, config, bot):
    service = service_for(bot)
    channel = bot.channels[LISTINGS]
    async with db.session() as session:
        await make_listing(session, config, 1)
    owner_message = await channel.send("## My own ad", allowed_mentions=discord.AllowedMentions.none())
    await service.adopt_self_post(1, owner_message)
    async with db.session() as session:
        controls_id = (await repository.get_listing(session, 1)).controls_message_id

    await service.publish_listing(1)  # e.g. a refresh

    async with db.session() as session:
        listing = await repository.get_listing(session, 1)
    assert listing.self_posted is False and listing.controls_message_id is None
    assert owner_message.id not in channel.messages and controls_id not in channel.messages
    assert channel.ids() == [listing.message_id, (await stored_panel(db)).message_id]

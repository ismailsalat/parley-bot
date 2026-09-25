"""Connected perks: a directory listing survives without Parley, perks don't.

The Discord objects here are stand-ins, so these check Parley's own decisions
(what stays live, what is gated, what is delivered) rather than Discord itself.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest

from bot.database import repository
from bot.database.models import ListingStatus
from bot.services import cooldowns, listings as listing_service, network, partnerships
from bot.services.errors import CooldownActive
from tests.factories import NOW, OWNER, make_listing
from tests.fakes import ADMIN_ID, MAIN, FakeBot

OTHER = 940000000000000002


def labels(view) -> list[str]:
    """Button labels on a view (selects have no label)."""
    return [label for c in view.children if (label := getattr(getattr(c, "item", c), "label", None))]


# ---------------------------------------------------------------- 1-3. leaving keeps the listing


async def test_removing_parley_keeps_the_directory_listing(db, config):
    """The listing is the server's, not a reward for keeping the bot installed."""
    bot = FakeBot(db)
    async with db.session() as session:
        await make_listing(session, config, MAIN, actor_id=ADMIN_ID)
        await network.configure(
            session, config, guild_id=MAIN, channel_id=55, categories=[], interval_minutes=180,
            enabled=True, actor_id=ADMIN_ID, now=NOW,
        )
        await network.set_auto_partner(session, guild_id=MAIN, enabled=True, actor_id=ADMIN_ID, now=NOW)

    deleted: list[tuple] = []
    bot.panels = SimpleNamespace(delete_listing_message=lambda c, m: deleted.append((c, m)) or _noop())
    logged: list[str] = []
    bot.log_event = lambda text: logged.append(text) or _noop()

    from bot.core import ParleyBot

    await ParleyBot.on_guild_remove(bot, SimpleNamespace(id=MAIN, name="Waypoint HQ"))

    async with db.session() as session:
        listing = await repository.get_listing(session, MAIN)
        settings = await repository.get_network_settings(session, MAIN)
    assert listing.status == ListingStatus.ACTIVE  # still in the directory
    assert listing.invite_url and listing.advertisement_text  # nothing of theirs was thrown away
    assert settings.enabled is False and settings.auto_partner is False  # perks off
    assert listing.partner_message_id is None  # connected-only partner post cleared
    assert "kept live" in logged[0] and "hidden" not in logged[0].lower()


def _noop():
    async def runner():
        return None

    return runner()


# ---------------------------------------------------------------- 4-5. listing manager state


async def test_listing_manager_shows_connection_state_and_cta(db, config):
    from bot.views.management import management_embed, management_view

    bot = FakeBot(db)
    bot.application_id = 123456789012345678
    async with db.session() as session:
        listing = await make_listing(session, config, MAIN, actor_id=ADMIN_ID)
    guild = bot.guild

    connected = management_embed(bot, listing, guild)
    assert any(f.name == "Parley" and f.value == "🟢 Connected" for f in connected.fields)
    assert "Connect Parley" not in labels(management_view(bot, listing, guild))

    gone = FakeBot(db)
    gone.application_id = 123456789012345678
    gone.get_guild = lambda _gid: None
    embed = management_embed(gone, listing, guild)
    assert any(f.name == "Parley" and f.value == "⚪ Not Connected" for f in embed.fields)
    assert "Connect Parley" in labels(management_view(gone, listing, guild))


# ---------------------------------------------------------------- 6-7. relist perk


async def test_connected_servers_relist_sooner(db, config):
    async with db.session() as session:
        listing = await make_listing(session, config, MAIN, actor_id=ADMIN_ID)

    standard = config.listings.refresh_cooldown_minutes
    connected = config.listings.connected_refresh_cooldown_minutes
    assert connected < standard

    # Standard listing: still waiting.
    assert listing_service.refresh_remaining(listing, config, NOW + timedelta(minutes=connected)) is not None
    # Same moment, connected: ready.
    assert listing_service.refresh_remaining(
        listing, config, NOW + timedelta(minutes=connected), connected=True
    ) is None

    with pytest.raises(CooldownActive):
        async with db.session() as session:
            await listing_service.claim_refresh(
                session, config, guild_id=MAIN, actor_id=OWNER, now=NOW + timedelta(minutes=connected)
            )
    async with db.session() as session:
        refreshed = await listing_service.claim_refresh(
            session, config, guild_id=MAIN, actor_id=OWNER, now=NOW + timedelta(minutes=connected), connected=True
        )
    assert refreshed.refreshed_at == NOW + timedelta(minutes=connected)


# ---------------------------------------------------------------- 8-9. connected-only perks


async def test_find_a_partner_needs_a_connected_source(db, config):
    from bot.views.partnership import start_find_flow
    from tests.fakes import FakeInteraction

    bot = FakeBot(db)
    bot.application_id = 123456789012345678
    async with db.session() as session:
        await make_listing(session, config, MAIN, actor_id=ADMIN_ID)
    bot.get_guild = lambda _gid: None  # listed, but Parley was removed
    interaction = FakeInteraction(bot, ADMIN_ID)
    await start_find_flow(interaction)
    message = interaction.response.sent[-1]
    assert "Parley Connected" in message["content"]
    assert "listing can stay live" in message["content"]
    assert "Add Parley" in labels(message["view"])


async def test_partner_posts_need_a_connected_source(db, config):
    from bot.views.partner_posts import _active_managed_listings

    bot = FakeBot(db)
    async with db.session() as session:
        await make_listing(session, config, MAIN, actor_id=ADMIN_ID)
    rows, _guilds = await _active_managed_listings(bot, ADMIN_ID)
    assert [row.guild_id for row in rows] == [MAIN]

    bot.get_guild = lambda _gid: None  # listing stays, perk does not
    rows, _guilds = await _active_managed_listings(bot, ADMIN_ID)
    assert rows == []


# ---------------------------------------------------------------- 10-11. request delivery


@pytest.fixture
async def pair(db, config):
    async with db.session() as session:
        await make_listing(session, config, MAIN, actor_id=ADMIN_ID, members=1850, categories=["Gaming"])
        await make_listing(session, config, OTHER, actor_id=OWNER)


async def make_request(db, config, source=MAIN, target=OTHER):
    async with db.session() as session:
        context = await partnerships.create_request(
            session, config, source_guild_id=source, target_guild_id=target, requester_id=ADMIN_ID,
            source_member_count=1850, message=None, now=NOW,
        )
        guilds = await repository.get_guilds(session, [source, target])
        return context, guilds


async def test_disconnected_target_gets_a_dm_without_accept_or_decline(db, config, pair):
    from bot.views.partnership import deliver_request_notification

    bot = FakeBot(db)
    bot.application_id = 123456789012345678
    bot.get_guild = lambda gid: bot.guild if gid == MAIN else None  # target is not connected
    context, guilds = await make_request(db, config)
    await deliver_request_notification(
        bot, context.request, guilds, context.source, context.target, [ADMIN_ID], actor_id=ADMIN_ID
    )
    dm = bot.users[ADMIN_ID].dms[-1]
    embed, view = dm["embed"], dm["view"]
    body = embed.description + " ".join(f"{f.name} {f.value}" for f in embed.fields)
    assert f"Server {MAIN}" in body and "Gaming" in body and "1,850" in body  # who, what, how big
    assert "sent you a partnership request" in body  # the requester's identity, not a faceless bot
    assert "to start the conversation" in body  # tells them to message the requester by hand
    assert "add parley" in body.lower()  # and how to get one-click access next time
    assert labels(view) == ["View Server", "Add Parley"]
    assert "Accept" not in labels(view) and "Decline" not in labels(view)


async def test_connected_target_gets_accept_and_decline(db, config, pair):
    from bot.views.partnership import request_actions

    bot = FakeBot(db)
    context, _guilds = await make_request(db, config)
    view = request_actions(bot, context.request)
    styles = {getattr(c, "item", c).label: getattr(c, "item", c).style for c in view.children}
    assert styles["Accept"] is discord.ButtonStyle.success
    assert styles["Decline"] is discord.ButtonStyle.danger
    assert styles["View Server"] is not discord.ButtonStyle.success


# ---------------------------------------------------------------- 14-15. pair cooldown


def test_pair_cooldown_key_is_symmetric():
    assert cooldowns.unordered_pair_key(2, 1) == cooldowns.unordered_pair_key(1, 2)
    assert cooldowns.unordered_pair_key(1, 2) != cooldowns.pair_key(2, 1)


async def test_pair_cooldown_starts_on_acceptance_and_blocks_both_directions(db, config, pair):
    context, _guilds = await make_request(db, config)
    async with db.session() as session:
        await partnerships.respond(
            session, config, request_id=context.request.id, responder_id=OWNER,
            responder_is_manager=True, accept=True, now=NOW,
        )
    soon = NOW + timedelta(hours=1)
    for source, target in ((MAIN, OTHER), (OTHER, MAIN)):  # order must not matter
        with pytest.raises(CooldownActive, match="partnered recently"):
            async with db.session() as session:
                await partnerships.create_request(
                    session, config, source_guild_id=source, target_guild_id=target, requester_id=ADMIN_ID,
                    source_member_count=1850, message=None, now=soon,
                )
    after = NOW + timedelta(hours=config.partnerships.pair_cooldown_hours, seconds=1)
    async with db.session() as session:
        await partnerships.create_request(
            session, config, source_guild_id=OTHER, target_guild_id=MAIN, requester_id=OWNER,
            source_member_count=500, message=None, now=after,
        )


# ---------------------------------------------------------------- 16-17. no more random ads


def test_random_network_ad_rotation_is_gone():
    import bot.tasks as tasks

    source = (tasks.__file__ and open(tasks.__file__, encoding="utf-8").read()) or ""
    assert "_post_to" not in source and "pick_candidate" not in source
    assert not hasattr(tasks.BackgroundTasks, "_post_to")
    assert hasattr(tasks.BackgroundTasks, "_auto_pair")


async def test_auto_partner_is_opt_in(db, config):
    """Network enabled is not Auto Partner enabled."""
    async with db.session() as session:
        settings = await network.configure(
            session, config, guild_id=MAIN, channel_id=55, categories=[], interval_minutes=180,
            enabled=True, actor_id=ADMIN_ID, now=NOW,
        )
        assert settings.auto_partner is False
        assert await network.due_destinations(session, config, NOW + timedelta(days=1)) == []

        await network.set_auto_partner(session, guild_id=MAIN, enabled=True, actor_id=ADMIN_ID, now=NOW)
        assert [s.guild_id for s in await network.due_destinations(session, config, NOW + timedelta(days=1))] == [MAIN]


async def test_leaving_the_network_also_turns_auto_partner_off(db, config):
    async with db.session() as session:
        await network.configure(
            session, config, guild_id=MAIN, channel_id=55, categories=[], interval_minutes=180,
            enabled=True, actor_id=ADMIN_ID, now=NOW,
        )
        await network.set_auto_partner(session, guild_id=MAIN, enabled=True, actor_id=ADMIN_ID, now=NOW)
        settings = await network.configure(
            session, config, guild_id=MAIN, channel_id=55, categories=[], interval_minutes=180,
            enabled=False, actor_id=ADMIN_ID, now=NOW,
        )
    assert settings.auto_partner is False


async def test_auto_partner_cannot_be_enabled_without_a_channel(db, config):
    from bot.services.errors import ValidationError

    with pytest.raises(ValidationError):
        async with db.session() as session:
            await network.set_auto_partner(session, guild_id=MAIN, enabled=True, actor_id=ADMIN_ID, now=NOW)


# ---------------------------------------------------------------- 22. schema matches the model


async def test_auto_partner_column_exists(db):
    async with db.session() as session:
        await network.repository.get_network_settings(session, MAIN)  # column is queryable
    from bot.database.models import NetworkSettings

    assert "auto_partner" in NetworkSettings.__table__.columns


# ---------------------------------------------------------------- 12-13. where a request is delivered


class ExchangeChannel:
    """A stand-in Network channel that records (and can refuse) sends."""

    def __init__(self, channel_id: int, guild_id: int, *, fail: bool = False) -> None:
        self.id = channel_id
        self.name = f"parley-network-{guild_id}"
        self.type = discord.ChannelType.text
        self.guild = SimpleNamespace(id=guild_id, me=SimpleNamespace(id=99))
        self.fail = fail
        self.sent: list[dict] = []
        self.deleted: list[int] = []

    def permissions_for(self, _member):
        return discord.Permissions(
            view_channel=True, send_messages=True, read_message_history=True, embed_links=True
        )

    async def send(self, **kwargs):
        if self.fail:
            raise discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "nope")
        self.sent.append(kwargs)
        channel = self

        class Sent:
            id = 5000 + len(channel.sent)

            async def delete(self_inner):
                channel.deleted.append(self_inner.id)

        return Sent()


def network_bot(db, channels: dict[int, ExchangeChannel]) -> FakeBot:
    bot = FakeBot(db)
    bot.application_id = 123456789012345678
    guilds = {gid: SimpleNamespace(id=gid, name=f"Server {gid}", me=SimpleNamespace(id=99)) for gid in channels}
    bot.get_guild = lambda gid: guilds.get(gid)
    bot.get_channel = lambda cid: next((c for c in channels.values() if c.id == cid), None)
    bot.log_event = lambda text: _noop()
    return bot


async def enable_network(db, config, guild_id: int, channel_id: int, *, auto: bool = False) -> None:
    async with db.session() as session:
        await network.configure(
            session, config, guild_id=guild_id, channel_id=channel_id, categories=[], interval_minutes=180,
            enabled=True, actor_id=ADMIN_ID, now=NOW,
        )
        if auto:
            await network.set_auto_partner(session, guild_id=guild_id, enabled=True, actor_id=ADMIN_ID, now=NOW)


async def test_connected_target_is_notified_in_its_network_channel(db, config, pair):
    from bot.views.partnership import deliver_request_notification

    target_channel = ExchangeChannel(701, OTHER)
    bot = network_bot(db, {MAIN: ExchangeChannel(700, MAIN), OTHER: target_channel})
    await enable_network(db, config, OTHER, target_channel.id)
    context, guilds = await make_request(db, config)

    await deliver_request_notification(
        bot, context.request, guilds, context.source, context.target, [ADMIN_ID], actor_id=ADMIN_ID
    )
    assert len(target_channel.sent) == 1
    assert labels(target_channel.sent[0]["view"]) == ["Accept", "Decline", "View Server"]
    assert bot.users[ADMIN_ID].dms == []  # no need to DM as well


async def test_delivery_falls_back_to_dm_when_the_channel_is_gone(db, config, pair):
    """A deleted channel must never swallow the request."""
    from bot.views.partnership import deliver_request_notification

    broken = ExchangeChannel(701, OTHER, fail=True)
    bot = network_bot(db, {MAIN: ExchangeChannel(700, MAIN), OTHER: broken})
    await enable_network(db, config, OTHER, broken.id)
    context, guilds = await make_request(db, config)

    await deliver_request_notification(
        bot, context.request, guilds, context.source, context.target, [ADMIN_ID], actor_id=ADMIN_ID
    )
    dm = bot.users[ADMIN_ID].dms[-1]
    # still Connected, so the one-click answer stays available
    assert labels(dm["view"]) == ["Accept", "Decline", "View Server"]


# ---------------------------------------------------------------- 20-21. mutual exchange


async def test_accepted_partnership_exchanges_both_ads(db, config, pair):
    from bot.views.partnership import exchange_partner_ads

    source_channel = ExchangeChannel(700, MAIN)
    target_channel = ExchangeChannel(701, OTHER)
    bot = network_bot(db, {MAIN: source_channel, OTHER: target_channel})
    await enable_network(db, config, MAIN, source_channel.id)
    await enable_network(db, config, OTHER, target_channel.id)

    assert await exchange_partner_ads(bot, MAIN, OTHER) is True
    assert len(source_channel.sent) == 1 and len(target_channel.sent) == 1
    # each server shows the *other* server's ad, with the exchange footer and no pings
    assert "partner exchange" in source_channel.sent[0]["content"]
    assert source_channel.sent[0]["allowed_mentions"].to_dict()["parse"] == []
    assert "embed" not in source_channel.sent[0]


async def test_one_sided_exchange_is_rolled_back(db, config, pair):
    """Never leave Server B advertised in Server A without the other direction."""
    from bot.views.partnership import exchange_partner_ads

    source_channel = ExchangeChannel(700, MAIN)
    target_channel = ExchangeChannel(701, OTHER, fail=True)
    bot = network_bot(db, {MAIN: source_channel, OTHER: target_channel})
    await enable_network(db, config, MAIN, source_channel.id)
    await enable_network(db, config, OTHER, target_channel.id)

    assert await exchange_partner_ads(bot, MAIN, OTHER) is False
    assert len(source_channel.sent) == 1
    assert source_channel.deleted == [5001]  # the first post was taken back down


async def test_exchange_refuses_unless_both_sides_can_receive(db, config, pair):
    from bot.views.partnership import exchange_partner_ads

    source_channel = ExchangeChannel(700, MAIN)
    target_channel = ExchangeChannel(701, OTHER)
    bot = network_bot(db, {MAIN: source_channel, OTHER: target_channel})
    await enable_network(db, config, MAIN, source_channel.id)  # only one side has a network channel

    assert await exchange_partner_ads(bot, MAIN, OTHER) is False
    assert source_channel.sent == [] and target_channel.sent == []

# ---------------------------------------------------------------- disconnected basic management


async def test_verified_manager_can_manage_after_parley_is_removed(db, config):
    from bot.services.errors import PermissionDenied
    from bot.views.management import load_managed_listing

    bot = FakeBot(db)
    bot.get_guild = lambda _gid: None
    async with db.session() as session:
        await make_listing(session, config, OTHER, actor_id=ADMIN_ID, contact_ids=[OWNER])

    guild, listing, contacts = await load_managed_listing(bot, OTHER, ADMIN_ID)
    assert guild.id == OTHER and guild.name == f"Server {OTHER}"
    assert listing.guild_id == OTHER
    assert OWNER in contacts

    # Partnership contacts are not automatically listing managers.
    with pytest.raises(PermissionDenied):
        await load_managed_listing(bot, OTHER, OWNER)


async def test_post_server_keeps_existing_disconnected_listings_reachable(db, config):
    """Post Server Ad still reaches an already-verified listing - and can list a new server.

    It used to open the Listing Manager directly, which dead-ended anyone who
    wanted to list a *second* server without installing Parley.
    """
    from bot.views.listings import start_post_flow
    from tests.fakes import FakeInteraction

    bot = FakeBot(db)
    bot.application_id = 123456789012345678
    bot.guilds = []
    bot.get_guild = lambda _gid: None
    async with db.session() as session:
        await make_listing(session, config, OTHER, actor_id=ADMIN_ID)

    interaction = FakeInteraction(bot, ADMIN_ID)
    await start_post_flow(interaction)

    sent = interaction.response.sent[-1]
    assert sent["embed"].title == "😊 Choose a Server"
    chooser = next(c for c in sent["view"].children if isinstance(c, discord.ui.Select))
    assert [option.value for option in chooser.options] == [str(OTHER)]  # the listing is one click away
    assert "Refresh Servers" in labels(sent["view"])  # and so is a brand new one

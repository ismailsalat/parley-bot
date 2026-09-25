"""Listing a server without installing Parley.

Covers the product rule (listing never requires the bot), and the security
rules around proving which servers someone manages.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import discord
import pytest

from bot.database import repository
from bot.database.models import ListingStatus, OAuthSession
from bot.services import verification
from bot.services.verification import VerifiedGuild
from tests.conftest import make_settings
from tests.factories import NOW, listing_input
from tests.fakes import ADMIN_ID, USER_ID, FakeBot, FakeInteraction

OWNED, ADMIN_GUILD, MANAGER_GUILD, PLAIN = 11, 22, 33, 44
REDIRECT = "https://parley.example/oauth/discord/callback"


def payload(guild_id: int, *, owner: bool = False, permissions: int = 0, name: str | None = None) -> dict:
    return {
        "id": str(guild_id),
        "name": name or f"Server {guild_id}",
        "icon": None,
        "owner": owner,
        "permissions": str(permissions),
        "approximate_member_count": 1234,
    }


ALL_GUILDS = [
    payload(OWNED, owner=True),
    payload(ADMIN_GUILD, permissions=discord.Permissions(administrator=True).value),
    payload(MANAGER_GUILD, permissions=discord.Permissions(manage_guild=True).value),
    payload(PLAIN, permissions=discord.Permissions(send_messages=True).value),
]


def oauth_bot(db, **overrides) -> FakeBot:
    bot = FakeBot(db)
    bot.application_id = 123456789012345678
    bot.settings = make_settings(
        "sqlite://",
        oauth_client_id=123456789012345678,
        oauth_client_secret="not-a-real-secret",
        oauth_redirect_uri=REDIRECT,
        **overrides,
    )
    return bot


# ---------------------------------------------------------------- 1-2. the reported bug


async def test_post_server_ad_without_the_bot_offers_verification(db):
    """The old 'Connect Parley once to list a new server' screen is gone."""
    from bot.views.listings import start_post_flow

    bot = oauth_bot(db)
    bot.get_guild = lambda _gid: None  # Parley is in nothing this user manages
    bot.guilds = []
    interaction = FakeInteraction(bot, ADMIN_ID)
    await start_post_flow(interaction)

    message = interaction.response.sent[-1]
    embed = message["embed"]
    assert "Connect Parley once" not in (embed.description or "")
    assert "Post Your Server" in (embed.title or "")
    assert "No bot required" in (embed.description or "")
    labels = [getattr(c, "item", c).label for c in message["view"].children]
    assert labels == ["Choose My Server", "Continue"]


async def test_the_legacy_copy_is_gone_from_the_codebase():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    hits = [
        path.relative_to(root).as_posix()
        for path in (root / "bot").rglob("*.py")
        if "Connect Parley once" in path.read_text(encoding="utf-8")
    ]
    assert hits == [], hits


async def test_verification_link_points_at_discord_with_the_right_scopes(db):
    from bot.views.listings import start_post_flow

    bot = oauth_bot(db)
    bot.get_guild = lambda _gid: None
    bot.guilds = []
    interaction = FakeInteraction(bot, ADMIN_ID)
    await start_post_flow(interaction)

    button = interaction.response.sent[-1]["view"].children[0]
    fields = parse_qs(urlparse(button.url).query)
    assert set(fields["scope"][0].split()) == {"identify", "guilds"}
    assert fields["redirect_uri"] == [REDIRECT]
    from sqlalchemy import select

    async with db.session() as session:
        row = await session.scalar(select(OAuthSession))
    assert fields["state"] == [row.state]  # the live session, not a guessable value


async def test_without_oauth_configured_it_says_so_instead_of_demanding_the_bot(db):
    from bot.views.listings import start_post_flow

    bot = FakeBot(db)
    bot.application_id = 1
    bot.get_guild = lambda _gid: None
    bot.guilds = []
    interaction = FakeInteraction(bot, ADMIN_ID)
    await start_post_flow(interaction)
    sent = interaction.response.sent[-1]
    assert "not ready yet" in sent["embed"].description


# ---------------------------------------------------------------- 3-7. state security


async def test_state_is_random_and_single_use(db):
    async with db.session() as session:
        first = await verification.start(session, user_id=ADMIN_ID, now=NOW)
        second = await verification.start(session, user_id=ADMIN_ID, now=NOW)
    assert first.state != second.state and len(first.state) >= 32

    async with db.session() as session:
        await verification.complete(session, state=first.state, oauth_user_id=ADMIN_ID, payloads=ALL_GUILDS, now=NOW)
    with pytest.raises(verification.VerificationError):  # replayed link
        async with db.session() as session:
            await verification.complete(session, state=first.state, oauth_user_id=ADMIN_ID, payloads=ALL_GUILDS, now=NOW)


async def test_expired_state_is_refused(db):
    async with db.session() as session:
        row = await verification.start(session, user_id=ADMIN_ID, now=NOW)
    later = NOW + verification.SESSION_TTL + timedelta(seconds=1)
    with pytest.raises(verification.VerificationError, match="expired"):
        async with db.session() as session:
            await verification.complete(session, state=row.state, oauth_user_id=ADMIN_ID, payloads=ALL_GUILDS, now=later)


async def test_unknown_state_is_refused(db):
    with pytest.raises(verification.VerificationError):
        async with db.session() as session:
            await verification.complete(session, state="made-up", oauth_user_id=ADMIN_ID, payloads=ALL_GUILDS, now=NOW)


async def test_another_account_cannot_complete_someone_elses_verification(db):
    async with db.session() as session:
        row = await verification.start(session, user_id=ADMIN_ID, now=NOW)
    with pytest.raises(verification.VerificationError, match="isn't the one"):
        async with db.session() as session:
            await verification.complete(session, state=row.state, oauth_user_id=USER_ID, payloads=ALL_GUILDS, now=NOW)
    # The link is spent even though the attempt failed, so it cannot be retried.
    with pytest.raises(verification.VerificationError):
        async with db.session() as session:
            await verification.complete(session, state=row.state, oauth_user_id=ADMIN_ID, payloads=ALL_GUILDS, now=NOW)
    async with db.session() as session:
        assert await verification.verified_guilds(session, user_id=ADMIN_ID, now=NOW) == []


# ---------------------------------------------------------------- 8-13. which guilds count


@pytest.mark.parametrize(
    ("guild", "eligible"),
    [(OWNED, True), (ADMIN_GUILD, True), (MANAGER_GUILD, True), (PLAIN, False)],
)
def test_only_servers_the_account_manages_are_eligible(guild, eligible):
    ids = {g.id for g in verification.eligible_guilds(ALL_GUILDS)}
    assert (guild in ids) is eligible


def test_member_counts_and_names_come_from_discord():
    owned = next(g for g in verification.eligible_guilds(ALL_GUILDS) if g.id == OWNED)
    assert owned.member_count == 1234 and owned.name == f"Server {OWNED}"


async def test_a_guild_id_is_never_taken_from_the_user(db):
    """Only guilds Discord vouched for can start a listing."""
    async with db.session() as session:
        row = await verification.start(session, user_id=ADMIN_ID, now=NOW)
        await verification.complete(session, state=row.state, oauth_user_id=ADMIN_ID, payloads=ALL_GUILDS, now=NOW)

    async with db.session() as session:
        allowed = await verification.require_verified_guild(session, user_id=ADMIN_ID, guild_id=OWNED, now=NOW)
        assert allowed.id == OWNED
        with pytest.raises(verification.VerificationError):  # a server they don't manage
            await verification.require_verified_guild(session, user_id=ADMIN_ID, guild_id=PLAIN, now=NOW)
        with pytest.raises(verification.VerificationError):  # someone else's verification
            await verification.require_verified_guild(session, user_id=USER_ID, guild_id=OWNED, now=NOW)


async def test_verification_goes_stale(db):
    async with db.session() as session:
        row = await verification.start(session, user_id=ADMIN_ID, now=NOW)
        await verification.complete(session, state=row.state, oauth_user_id=ADMIN_ID, payloads=ALL_GUILDS, now=NOW)
    stale = NOW + verification.VERIFIED_TTL + timedelta(seconds=1)
    async with db.session() as session:
        assert await verification.verified_guilds(session, user_id=ADMIN_ID, now=stale) == []


# ---------------------------------------------------------------- 14-17. invites without the bot


class FakeInvite:
    def __init__(self, guild_id: int | None, code: str = "abc123") -> None:
        self.code = code
        self.guild = SimpleNamespace(id=guild_id) if guild_id else None


async def test_invite_must_point_at_the_verified_server(db):
    from bot.views.listings import resolve_verified_invite

    bot = oauth_bot(db)
    bot.fetch_invite = lambda code, with_counts=False: _returns(FakeInvite(OWNED))
    assert await resolve_verified_invite(bot, OWNED, "discord.gg/abc123") == "https://discord.gg/abc123"

    bot.fetch_invite = lambda code, with_counts=False: _returns(FakeInvite(PLAIN))
    with pytest.raises(Exception, match="different server"):
        await resolve_verified_invite(bot, OWNED, "discord.gg/abc123")


async def test_invalid_invite_is_rejected(db):
    from bot.views.listings import resolve_verified_invite

    bot = oauth_bot(db)

    async def missing(code, with_counts=False):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Invite")

    bot.fetch_invite = missing
    with pytest.raises(Exception, match="invalid or has expired"):
        await resolve_verified_invite(bot, OWNED, "discord.gg/gone")


async def test_no_invite_is_created_while_disconnected(db):
    """Parley must not try to make an invite in a server it isn't in."""
    from bot.views.listings import InviteMissing, resolve_verified_invite

    bot = oauth_bot(db)
    called = []
    bot.fetch_invite = lambda code, with_counts=False: called.append(code) or _returns(FakeInvite(OWNED))
    with pytest.raises(InviteMissing):
        await resolve_verified_invite(bot, OWNED, "")  # nothing pasted
    assert called == []  # and nothing was created either


def _returns(value):
    async def runner():
        return value

    return runner()


# ---------------------------------------------------------------- 18-21. the listing itself


async def test_a_listing_can_be_created_with_the_bot_absent(db, config):
    """The whole point: a directory listing without installing Parley."""
    from bot.services import listings as listing_service
    from bot.views.verify import verified_guild_info

    verified = VerifiedGuild(id=OWNED, name="Botless HQ", icon_url=None, member_count=1234)
    async with db.session() as session:
        listing = await listing_service.create_listing(
            session, config, guild=verified_guild_info(verified), actor_id=ADMIN_ID,
            data=listing_input(contact_ids=[ADMIN_ID]), now=NOW,
        )
    assert listing.status == ListingStatus.ACTIVE
    async with db.session() as session:
        stored = await repository.get_guild(session, OWNED)
        contacts = await repository.get_contact_ids(session, OWNED)
    assert stored.name == "Botless HQ" and stored.member_count == 1234
    assert contacts == [ADMIN_ID]  # the verifying manager, nobody else


async def test_connected_perks_stay_unavailable_until_parley_is_installed(db, config):
    from bot.services import listings as listing_service
    from bot.services import permissions
    from bot.views.verify import verified_guild_info

    bot = oauth_bot(db)
    bot.get_guild = lambda _gid: None
    verified = VerifiedGuild(id=OWNED, name="Botless HQ", member_count=10)
    async with db.session() as session:
        await listing_service.create_listing(
            session, config, guild=verified_guild_info(verified), actor_id=ADMIN_ID,
            data=listing_input(contact_ids=[ADMIN_ID]), now=NOW,
        )

    assert permissions.is_connected(bot, OWNED) is False
    # standard cooldown, not the Connected one
    async with db.session() as session:
        listing = await repository.get_listing(session, OWNED)
    standard = config.listings.refresh_cooldown_minutes
    connected = config.listings.connected_refresh_cooldown_minutes
    assert listing_service.refresh_remaining(listing, config, NOW + timedelta(minutes=connected)) is not None
    assert listing_service.refresh_remaining(listing, config, NOW + timedelta(minutes=standard)) is None

    # installing Parley later flips it, with no re-listing
    bot.get_guild = lambda gid: SimpleNamespace(id=gid)
    assert permissions.is_connected(bot, OWNED) is True


async def test_a_botless_listing_is_still_manageable(db, config):
    """Disconnected management must keep working for these listings."""
    from bot.services import listings as listing_service
    from bot.views.verify import verified_guild_info

    verified = VerifiedGuild(id=OWNED, name="Botless HQ", member_count=10)
    async with db.session() as session:
        await listing_service.create_listing(
            session, config, guild=verified_guild_info(verified), actor_id=ADMIN_ID,
            data=listing_input(contact_ids=[ADMIN_ID]), now=NOW,
        )
    async with db.session() as session:
        await listing_service.update_advertisement(
            session, config, guild_id=OWNED, text="**Edited without the bot**", actor_id=ADMIN_ID, now=NOW
        )
        assert await repository.guild_ids_connected_by(session, ADMIN_ID) == [OWNED]
    async with db.session() as session:
        assert (await repository.get_listing(session, OWNED)).advertisement_text == "**Edited without the bot**"


# ---------------------------------------------------------------- 25. live panels pick this up


async def test_repair_panels_rerenders_the_live_panel(db):
    """Old panel messages must not keep pointing at the legacy flow."""
    from bot.views.welcome import listings_panel, welcome_panel

    bot = oauth_bot(db)
    for builder in (welcome_panel, listings_panel):
        _content, view = builder(bot)
        posts = [c for c in view.children if getattr(getattr(c, "item", c), "custom_id", "") == "wp:act:post"]
        assert posts, "Post Server Ad button missing from the panel"

    # Repair Panels / startup restore re-render the same builders with force_edit,
    # so an existing message is edited in place rather than left stale.
    import inspect

    from bot.services.panels import PanelService

    assert "force_edit" in inspect.signature(PanelService.restore_panels).parameters
    assert "force_edit" in inspect.signature(PanelService.ensure_panel).parameters


async def test_the_post_button_routes_to_the_new_flow():
    """The custom_id is unchanged, so buttons on old messages keep working: only the handler moved."""
    from bot.views.listings import start_post_flow
    from bot.views.welcome import _HANDLERS, ActionButton, registered_actions

    assert "post" in registered_actions()
    assert _HANDLERS["post"] is start_post_flow  # the same button now starts verification
    assert ActionButton("post").item.custom_id == "wp:act:post"


# ---------------------------------------------------------------- claim atomicity


async def test_only_one_claim_of_a_state_can_win(db):
    """Two callbacks arriving together: the database picks exactly one winner."""
    import asyncio

    async with db.session() as session:
        row = await verification.start(session, user_id=ADMIN_ID, now=NOW)

    async def attempt():
        try:
            async with db.session() as session:
                await verification.complete(
                    session, state=row.state, oauth_user_id=ADMIN_ID, payloads=ALL_GUILDS, now=NOW
                )
            return "won"
        except verification.VerificationError:
            return "refused"

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == ["refused", "won"]


async def test_a_state_burned_by_the_wrong_account_cannot_be_reused(db):
    async with db.session() as session:
        row = await verification.start(session, user_id=ADMIN_ID, now=NOW)
    with pytest.raises(verification.VerificationError):
        async with db.session() as session:
            await verification.complete(session, state=row.state, oauth_user_id=USER_ID, payloads=ALL_GUILDS, now=NOW)
    with pytest.raises(verification.VerificationError):  # even by the rightful owner
        async with db.session() as session:
            await verification.complete(session, state=row.state, oauth_user_id=ADMIN_ID, payloads=ALL_GUILDS, now=NOW)


# ---------------------------------------------------------------- pending vs zero servers


async def test_pending_and_zero_server_verifications_are_different_answers(db):
    async with db.session() as session:
        pending = await verification.latest_verification(session, user_id=ADMIN_ID, now=NOW)
    assert pending.completed is False and pending.guilds == []

    async with db.session() as session:
        row = await verification.start(session, user_id=ADMIN_ID, now=NOW)
        await verification.complete(
            session, state=row.state, oauth_user_id=ADMIN_ID, payloads=[payload(PLAIN)], now=NOW
        )
    async with db.session() as session:
        done = await verification.latest_verification(session, user_id=ADMIN_ID, now=NOW)
    assert done.completed is True and done.guilds == []  # verified, but manages nothing


async def test_zero_managed_servers_says_so_instead_of_asking_again(db):
    from bot.utils.helpers import utcnow
    from bot.views.verify import show_verified_servers

    bot = oauth_bot(db)
    now = utcnow()  # the view reads the real clock
    async with db.session() as session:
        row = await verification.start(session, user_id=ADMIN_ID, now=now)
        await verification.complete(
            session, state=row.state, oauth_user_id=ADMIN_ID, payloads=[payload(PLAIN)], now=now
        )
    interaction = FakeInteraction(bot, ADMIN_ID)
    await show_verified_servers(interaction)
    sent = interaction.response.sent[-1]
    assert "No servers found" in sent["embed"].title
    assert "hasn't seen a completed verification" not in (sent["embed"].description or "")
    assert [getattr(c, "item", c).label for c in sent["view"].children] == ["Choose Again", "Home"]


async def test_pending_verification_still_asks_to_finish_in_the_browser(db):
    from bot.views.verify import show_verified_servers

    bot = oauth_bot(db)
    interaction = FakeInteraction(bot, ADMIN_ID)
    await show_verified_servers(interaction)
    assert "Still waiting for Discord" in interaction.response.sent[-1]["embed"].description


# ---------------------------------------------------------------- always a route to a new server


async def test_a_connected_server_never_hides_the_botless_route(db, config):
    """Owning a listed server must not trap you into installing Parley for the next one."""
    from bot.views.listings import start_post_flow

    bot = oauth_bot(db)
    everyone = SimpleNamespace(id=OWNED)
    channel = SimpleNamespace(
        id=1, name="general", type=discord.ChannelType.text,
        permissions_for=lambda _target: discord.Permissions(view_channel=True, send_messages=True),
    )
    connected = SimpleNamespace(
        id=OWNED, name="Connected HQ", member_count=50, me=SimpleNamespace(id=99),
        default_role=everyone, text_channels=[channel], rules_channel=None, system_channel=None,
    )
    bot.get_guild = lambda gid: connected if gid == OWNED else None
    bot.guilds = [connected]
    import bot.services.permissions as perms

    original = perms.cached_manageable_guilds
    perms.cached_manageable_guilds = lambda _bot, _uid: [connected]
    try:
        interaction = FakeInteraction(bot, ADMIN_ID)
        await start_post_flow(interaction)
    finally:
        perms.cached_manageable_guilds = original

    view = interaction.response.sent[-1]["view"]
    labels = [c.label for c in view.children if getattr(c, "label", None)]
    assert "Add Another Server" in labels


# ---------------------------------------------------------------- 10. the whole journey


async def _verify(db, user_id: int = ADMIN_ID) -> None:
    """Complete a verification on the real clock, as the views read it."""
    from bot.utils.helpers import utcnow

    now = utcnow()
    async with db.session() as session:
        row = await verification.start(session, user_id=user_id, now=now)
        await verification.complete(session, state=row.state, oauth_user_id=user_id, payloads=ALL_GUILDS, now=now)


def _botless_bot(db):
    bot = oauth_bot(db)
    bot.get_guild = lambda _gid: None  # Parley is in none of the user's servers
    bot.guilds = []
    bot.fetch_invite = lambda code, with_counts=False: _returns(FakeInvite(OWNED))
    return bot


async def test_journey_verify_pick_publish_and_manage_without_the_bot(db, config):
    """Post Server Ad -> verify -> pick -> invite -> publish -> manage, bot absent throughout."""
    from bot.views.listings import ListingFormView, start_post_flow
    from bot.views.verify import show_verified_servers

    bot = _botless_bot(db)
    published: list[int] = []
    bot.panels = SimpleNamespace(
        publish_listing=lambda gid: _returns(SimpleNamespace(jump_url="https://discord.com/x", id=1))
        or published.append(gid),
        listings_channel=lambda: None,
    )

    # 1. the entry point offers verification, not an install
    first = FakeInteraction(bot, ADMIN_ID)
    await start_post_flow(first)
    assert "Choose My Server" in [c.label for c in first.response.sent[-1]["view"].children if getattr(c, "label", None)]

    # 2. OAuth completes out of band, then Continue shows the picker
    await _verify(db)
    second = FakeInteraction(bot, ADMIN_ID)
    await show_verified_servers(second)
    options = [o.value for o in next(iter(
        c for c in second.response.sent[-1]["view"].children if isinstance(c, discord.ui.Select)
    )).options]
    assert str(OWNED) in options and str(PLAIN) not in options

    # 3. the form is botless, and the invite is checked against the verified server
    verified = VerifiedGuild(id=OWNED, name="Botless HQ", member_count=1234)
    form = ListingFormView(
        bot, ADMIN_ID, OWNED, verified.name, _draft(), mode="create", in_dm=True, verified=verified
    )
    third = FakeInteraction(bot, ADMIN_ID)
    await form.continue_to_preview(third)
    assert form.draft.invite_url == "https://discord.gg/abc123"

    # 4. publishing works with no live guild anywhere
    listing = await form.create_only(FakeInteraction(bot, ADMIN_ID))
    assert listing.status == ListingStatus.ACTIVE

    # 5. and the listing is manageable afterwards, still with no bot
    async with db.session() as session:
        assert await repository.guild_ids_connected_by(session, ADMIN_ID) == [OWNED]
        from bot.services import listings as listing_service

        await listing_service.update_advertisement(
            session, config, guild_id=OWNED, text="**Edited**", actor_id=ADMIN_ID, now=NOW
        )
    async with db.session() as session:
        assert (await repository.get_listing(session, OWNED)).advertisement_text == "**Edited**"


def _draft():
    from bot.views.listings import ListingDraft

    return ListingDraft(
        categories=["Gaming"], accepting=True, contacts=[ADMIN_ID],
        ad_text="## Botless HQ\nCome join us!", invite_raw="discord.gg/abc123",
    )


async def test_paste_my_own_ad_works_without_the_bot(db, config):
    """The blocker: direct posting used to demand bot.get_guild(target)."""
    from bot.views.listings import ListingFormView

    bot = _botless_bot(db)
    await _verify(db)
    captured: dict = {}

    async def fake_run_submission(interaction, guild_id, guild_name, **kwargs):
        captured.update(guild_id=guild_id, guild_name=guild_name, authorize=kwargs.get("authorize"))
        await kwargs["on_accept"]("## Posted by the owner", SimpleNamespace(id=99))
        return "posted"

    import bot.views.self_post as self_post_module

    original = self_post_module.run_submission
    self_post_module.run_submission = fake_run_submission
    adopted: list[int] = []
    bot.panels = SimpleNamespace(
        adopt_self_post=lambda gid, message: adopted.append(gid) or _returns(None),
        listings_channel=lambda: None,
    )
    bot.log_event = lambda text: _returns(None)
    try:
        verified = VerifiedGuild(id=OWNED, name="Botless HQ", member_count=1234)
        form = ListingFormView(
            bot, ADMIN_ID, OWNED, verified.name, _draft(), mode="create", in_dm=True, verified=verified
        )
        interaction = FakeInteraction(bot, ADMIN_ID)
        await form.start_self_post(interaction)
    finally:
        self_post_module.run_submission = original

    assert captured["guild_id"] == OWNED
    assert captured["guild_name"] == "Botless HQ"  # the verified name, not a live guild's
    assert captured["authorize"] is not None  # OAuth authority replaced the gateway check
    assert adopted == [OWNED]
    async with db.session() as session:
        assert (await repository.get_listing(session, OWNED)) is not None


async def test_the_connected_path_still_uses_the_gateway_manager_check(db, config):
    """Injecting authorization must not have loosened the normal path."""
    import inspect

    from bot.views.self_post import run_submission

    assert inspect.signature(run_submission).parameters["authorize"].default is None
    source = inspect.getsource(run_submission)
    assert "(authorize or permissions.require_manager)(bot, guild_id, interaction.user.id)" in source

    from bot.views.listings import ListingFormView

    bot = oauth_bot(db)
    connected_form = ListingFormView(
        bot, ADMIN_ID, OWNED, "Connected HQ", _draft(), mode="create", in_dm=True
    )
    assert connected_form.verified is None  # so start_self_post passes authorize=None

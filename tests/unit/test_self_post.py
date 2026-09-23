"""Paste My Own Ad: authorisation, a single message, moderation, and cleanup every time.

The Discord channel and member are stand-ins, so these check Parley's own
rules (who may post, what is granted/revoked, what is stored). Real posting is
covered by ``python -m bot.tools.live_check``.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import discord
import pytest

from bot.database import repository
from bot.database.models import ListingStatus
from bot.services.errors import ValidationError
from bot.views import self_post
from tests.factories import make_listing
from tests.fakes import ADMIN_ID, MAIN, USER_ID, FakeBot, FakeInteraction

CHANNEL = 970000000000000001


class FakeMessage:
    def __init__(self, channel, author_id: int, content: str, message_id: int = 5001) -> None:
        self.channel = channel
        self.author = SimpleNamespace(id=author_id)
        self.content = content
        self.id = message_id
        self.jump_url = f"https://discord.com/channels/{MAIN}/{CHANNEL}/{message_id}"
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class FakeChannel:
    def __init__(self, guild, *, manage: bool = True) -> None:
        self.id = CHANNEL
        self.name = "server-directory"
        self.guild = guild
        self.mention = f"<#{CHANNEL}>"
        self.permission_calls: list[tuple[int, object]] = []
        self._perms = discord.Permissions(
            view_channel=True, send_messages=True, read_message_history=True, embed_links=True,
            manage_messages=manage, manage_roles=manage,
        )
        self.sent: list[dict] = []

    def permissions_for(self, _member):
        return self._perms

    async def set_permissions(self, target, *, overwrite=..., reason=None, **kwargs):
        self.permission_calls.append((target.id, kwargs if kwargs else overwrite))

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return FakeMessage(self, 0, "", message_id=9000 + len(self.sent))


def make_bot(db, *, manage: bool = True, message_content: bool = True, allow: bool = True) -> FakeBot:
    bot = FakeBot(db, message_content=message_content)
    if not allow:
        bot.runtime = replace(bot.runtime, listings=replace(bot.runtime.listings, allow_self_post=False))
    guild = bot.guild
    channel = FakeChannel(guild, manage=manage)
    guild.me = SimpleNamespace(id=99)
    guild.get_member = lambda uid: guild.members.get(uid)
    bot.channel = channel
    bot.panels = SimpleNamespace(
        listings_channel=lambda: channel,
        adopt_self_post=_recorder(bot),
    )
    bot.adopted = []
    return bot


def _recorder(bot):
    async def adopt(guild_id, message):
        bot.adopted.append((guild_id, message.id))

    return adopt


def queue_message(bot: FakeBot, message, *, delay: float = 0) -> None:
    """The owner's message arrives while Parley waits."""

    async def wait_for(_event, *, check, timeout):
        await asyncio.sleep(delay)
        if message is None or not check(message):
            raise asyncio.TimeoutError
        return message

    bot.wait_for = wait_for


@pytest.fixture
async def listed(db, config):
    async with db.session() as session:
        await make_listing(session, config, MAIN, actor_id=ADMIN_ID)


# ---------------------------------------------------------------- availability


async def test_offered_only_when_it_can_work_safely(db):
    assert self_post.availability(make_bot(db), MAIN).ok
    assert not self_post.availability(make_bot(db, message_content=False), MAIN).ok
    assert "Message Content" in self_post.availability(make_bot(db, message_content=False), MAIN).reason
    assert not self_post.availability(make_bot(db, manage=False), MAIN).ok
    assert "Manage Messages" in self_post.availability(make_bot(db, manage=False), MAIN).reason
    assert not self_post.availability(make_bot(db, allow=False), MAIN).ok


async def test_active_window_does_not_disable_the_whole_channel(db):
    bot = make_bot(db)
    self_post._ACTIVE[(CHANNEL, ADMIN_ID)] = self_post.ActiveWindow(ADMIN_ID, MAIN)
    try:
        # Availability is about bot/channel capability, not another user's window.
        assert self_post.availability(bot, MAIN).ok
    finally:
        self_post._ACTIVE.pop((CHANNEL, ADMIN_ID), None)
    assert self_post.availability(bot, MAIN).ok


async def test_different_servers_can_have_concurrent_windows(db):
    bot = make_bot(db)
    other_user = USER_ID
    other_guild = MAIN + 123
    self_post._ACTIVE[(CHANNEL, ADMIN_ID)] = self_post.ActiveWindow(ADMIN_ID, MAIN)
    self_post._ACTIVE[(CHANNEL, other_user)] = self_post.ActiveWindow(other_user, other_guild)
    try:
        assert self_post.is_active_submission(CHANNEL, ADMIN_ID)
        assert self_post.is_active_submission(CHANNEL, other_user)
        assert self_post.availability(bot, MAIN).ok
    finally:
        self_post._ACTIVE.clear()


# ---------------------------------------------------------------- authorisation


async def test_only_the_listing_manager_may_post(db, listed):
    bot = make_bot(db)
    interaction = FakeInteraction(bot, USER_ID)  # a normal member, no Manage Server
    interaction.edit_original_response = _noop
    queue_message(bot, FakeMessage(bot.channel, USER_ID, "Sneaky ad"))
    with pytest.raises(Exception) as caught:
        await self_post.run_submission(interaction, MAIN, "Parley HQ")
    assert "Manage Server" in getattr(caught.value, "user_message", str(caught.value))
    assert bot.channel.permission_calls == []  # nothing was ever granted


async def test_refused_before_granting_when_unavailable(db, listed):
    bot = make_bot(db, manage=False)
    interaction = FakeInteraction(bot, ADMIN_ID)
    interaction.edit_original_response = _noop
    with pytest.raises(ValidationError):
        await self_post.run_submission(interaction, MAIN, "Parley HQ")
    assert bot.channel.permission_calls == []


# ---------------------------------------------------------------- the window


async def _noop(**kwargs):
    return None


async def run(bot, message, guild_id=MAIN):
    interaction = FakeInteraction(bot, ADMIN_ID)
    interaction.edit_original_response = _noop
    queue_message(bot, message)
    return await self_post.run_submission(interaction, guild_id, "Parley HQ")


async def test_accepted_message_becomes_the_listing(db, listed):
    bot = make_bot(db)
    message = FakeMessage(bot.channel, ADMIN_ID, "## Rivals HQ <:crown:123456789012345678>\nJoin us!")
    note = await run(bot, message)
    assert "live" in note.lower() or message.jump_url in note
    assert bot.adopted == [(MAIN, message.id)]  # Parley adopted the owner's own message
    assert not message.deleted
    async with db.session() as session:
        listing = await repository.get_listing(session, MAIN)
    assert listing.advertisement_text.startswith("## Rivals HQ")


async def test_permission_is_granted_then_always_revoked(db, listed):
    bot = make_bot(db)
    await run(bot, FakeMessage(bot.channel, ADMIN_ID, "A clean ad"))
    granted, revoked = bot.channel.permission_calls
    assert granted[0] == ADMIN_ID and granted[1].send_messages is True
    assert revoked[0] == ADMIN_ID and revoked[1] is None  # overwrite restored/removed again
    async with db.session() as session:
        assert await repository.get_self_post_session(session, CHANNEL, ADMIN_ID) is None


async def test_posting_window_ghost_pings_only_the_person_who_opened_it(db, listed):
    bot = make_bot(db)
    await run(bot, FakeMessage(bot.channel, ADMIN_ID, "A clean ad"))
    assert bot.channel.sent, "the posting window should create a temporary notification"
    ping = bot.channel.sent[0]
    assert ping["content"] == f"<@{ADMIN_ID}>"
    mentions = ping["allowed_mentions"].to_dict()
    assert mentions.get("parse") == []
    assert mentions.get("users") == [ADMIN_ID]


async def test_abandoned_permission_window_is_recovered_on_restart(db, listed):
    bot = make_bot(db)
    bot.get_channel = lambda channel_id: bot.channel if channel_id == CHANNEL else None
    member = bot.guild.members[ADMIN_ID]

    async def fetch_member(user_id):
        assert user_id == ADMIN_ID
        return member

    bot.guild.fetch_member = fetch_member
    async with db.session() as session:
        await repository.save_self_post_session(
            session,
            channel_id=CHANNEL,
            hub_guild_id=MAIN,
            listing_guild_id=MAIN,
            user_id=ADMIN_ID,
            had_overwrite=False,
            previous_allow=0,
            previous_deny=0,
            started_at=self_post.utcnow(),
            expires_at=self_post.utcnow(),
        )

    assert await self_post.restore_abandoned_sessions(bot) == 1
    assert bot.channel.permission_calls[-1] == (ADMIN_ID, None)
    async with db.session() as session:
        assert await repository.get_self_post_session(session, CHANNEL, ADMIN_ID) is None


async def test_timeout_closes_the_window_and_changes_nothing(db, listed):
    bot = make_bot(db)
    interaction = FakeInteraction(bot, ADMIN_ID)
    interaction.edit_original_response = _noop
    queue_message(bot, None)  # nobody posts
    note = await self_post.run_submission(interaction, MAIN, "Parley HQ")
    assert "expired" in note.lower()
    assert bot.adopted == []
    assert bot.channel.permission_calls[-1][1] is None  # revoked anyway


async def test_only_the_first_message_is_taken(db, listed):
    """The window closes on the first accepted message, so a second one is never adopted."""
    bot = make_bot(db)
    first = FakeMessage(bot.channel, ADMIN_ID, "First ad", message_id=1)
    await run(bot, first)
    assert bot.adopted == [(MAIN, 1)]
    assert (CHANNEL, ADMIN_ID) not in self_post._ACTIVE  # and the window is closed again


async def test_messages_from_other_people_are_ignored(db, listed):
    bot = make_bot(db)
    note = await run(bot, FakeMessage(bot.channel, USER_ID, "not the owner"))
    assert "expired" in note.lower() and bot.adopted == []


# ---------------------------------------------------------------- moderation


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("Free nitro http://grabify.link/abc", "grabify.link"),
        (" ".join(f"https://s{i}.com" for i in range(7)), "at most 5"),
        ("   ", "empty"),
    ],
)
async def test_unsafe_submissions_are_deleted_and_explained(db, listed, content, reason):
    bot = make_bot(db)
    message = FakeMessage(bot.channel, ADMIN_ID, content)
    note = await run(bot, message)
    assert message.deleted
    assert reason in note and bot.adopted == []
    assert bot.channel.permission_calls[-1][1] is None


async def test_blocked_words_apply_to_submitted_ads(db, listed):
    bot = make_bot(db)
    bot.runtime = replace(bot.runtime, moderation=replace(bot.runtime.moderation, blocked_words=("scam",)))
    message = FakeMessage(bot.channel, ADMIN_ID, "Totally not a SCAM")
    note = await run(bot, message)
    assert message.deleted and "isn't allowed" in note


async def test_approval_holds_the_ad_instead_of_leaving_it_up(db, listed):
    bot = make_bot(db)
    bot.runtime = replace(bot.runtime, listings=replace(bot.runtime.listings, approval_required=True))
    message = FakeMessage(bot.channel, ADMIN_ID, "## Pending ad")
    note = await run(bot, message)
    assert message.deleted  # it can't stay visible while staff decide
    assert "review" in note and bot.adopted == []
    async with db.session() as session:
        listing = await repository.get_listing(session, MAIN)
    assert listing.pending_changes == {"advertisement_text": "## Pending ad"}
    assert listing.status == ListingStatus.ACTIVE  # the old approved ad stays live


async def test_suspicious_links_are_reviewed_not_published(db, listed):
    bot = make_bot(db)
    message = FakeMessage(bot.channel, ADMIN_ID, "join bit.ly/rivals")
    note = await run(bot, message)
    assert message.deleted and "review" in note and bot.adopted == []

"""Partnership requests: self/duplicate prevention, accept/decline, cooldowns."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from bot.database import repository
from bot.database.models import PartnershipRequest, RequestStatus
from bot.services import partnerships
from bot.services.errors import Conflict, CooldownActive, NotFound, PermissionDenied, ValidationError
from tests.factories import NOW, make_listing

SOURCE, TARGET, OTHER = 200000000000000001, 200000000000000002, 200000000000000003
REQUESTER, TARGET_OWNER = 5001, 5002


@pytest.fixture
async def servers(db, config):
    async with db.session() as session:
        await make_listing(session, config, SOURCE, actor_id=REQUESTER, members=300)
        await make_listing(session, config, TARGET, actor_id=TARGET_OWNER, members=430, minimum_members=100)
        await make_listing(session, config, OTHER, actor_id=9999)


async def request(db, config, *, source=SOURCE, target=TARGET, requester=REQUESTER, members=300, now=NOW, message="Hi!"):
    async with db.session() as session:
        ctx = await partnerships.create_request(
            session,
            config,
            source_guild_id=source,
            target_guild_id=target,
            requester_id=requester,
            source_member_count=members,
            message=message,
            now=now,
        )
        return ctx.request.id


async def respond(db, config, request_id, *, accept, responder=TARGET_OWNER, manager=False, now=NOW):
    async with db.session() as session:
        return await partnerships.respond(
            session,
            config,
            request_id=request_id,
            responder_id=responder,
            responder_is_manager=manager,
            accept=accept,
            now=now,
        )


async def test_request_is_created(db, config, servers):
    request_id = await request(db, config)
    async with db.session() as session:
        stored = await repository.get_request(session, request_id)
        assert stored.status == RequestStatus.PENDING
        assert stored.message == "Hi!"


async def test_self_partnership_is_rejected(db, config, servers):
    with pytest.raises(ValidationError, match="itself"):
        await request(db, config, target=SOURCE)


async def test_self_partnership_blocked_by_database_constraint(db, config, servers):
    with pytest.raises(IntegrityError):
        async with db.session() as session:
            session.add(PartnershipRequest(source_guild_id=SOURCE, target_guild_id=SOURCE, requester_user_id=1))
            await session.flush()


async def test_duplicate_pending_request_is_rejected(db, config, servers):
    await request(db, config)
    with pytest.raises(Conflict, match="already has a pending partnership request"):
        await request(db, config, requester=7777, now=NOW + timedelta(minutes=10))


async def test_duplicate_pending_blocked_by_database_index(db, config, servers):
    await request(db, config)
    with pytest.raises(IntegrityError):
        async with db.session() as session:
            session.add(
                PartnershipRequest(source_guild_id=SOURCE, target_guild_id=TARGET, requester_user_id=1, status="pending")
            )
            await session.flush()


async def test_reverse_pending_request_points_to_inbox(db, config, servers):
    await request(db, config)
    with pytest.raises(Conflict, match="already sent .* a partnership request"):
        await request(db, config, source=TARGET, target=SOURCE, requester=TARGET_OWNER, members=430)


async def test_not_accepting_target_is_rejected(db, config, servers):
    async with db.session() as session:
        listing = await repository.get_listing(session, TARGET)
        listing.accepting_partnerships = False
    with pytest.raises(ValidationError, match="This server is not accepting partnerships."):
        await request(db, config)


async def test_minimum_members_is_enforced(db, config, servers):
    with pytest.raises(ValidationError, match="at least 100"):
        await request(db, config, members=50)


async def test_missing_target_listing(db, config, servers):
    with pytest.raises(NotFound, match="This listing no longer exists."):
        await request(db, config, target=123456789012345678)


async def test_requester_cooldown_limits_spam(db, config, servers):
    from dataclasses import replace

    config = replace(config, partnerships=replace(config.partnerships, server_request_cooldown_seconds=0))
    await request(db, config)
    with pytest.raises(CooldownActive, match="before sending another request"):
        await request(db, config, target=OTHER, now=NOW + timedelta(seconds=10))
    later = NOW + timedelta(seconds=config.partnerships.request_cooldown_seconds)
    await request(db, config, target=OTHER, now=later)


async def test_server_cooldown_is_shared_across_admins(db, config, servers):
    """A second admin cannot immediately fire another request for the same source server."""
    from dataclasses import replace

    config = replace(config, partnerships=replace(config.partnerships, request_cooldown_seconds=0))
    await request(db, config)
    with pytest.raises(CooldownActive, match="Another admin may have sent it"):
        await request(
            db, config, source=SOURCE, target=OTHER, requester=7777,
            now=NOW + timedelta(seconds=10),
        )
    later = NOW + timedelta(seconds=config.partnerships.server_request_cooldown_seconds + 1)
    await request(db, config, source=SOURCE, target=OTHER, requester=7777, now=later)


async def test_accept_notifies_state_and_second_click_loses(db, config, servers):
    request_id = await request(db, config)
    accepted = await respond(db, config, request_id, accept=True)
    assert accepted.status == RequestStatus.ACCEPTED
    assert accepted.responded_by == TARGET_OWNER
    with pytest.raises(Conflict, match="already accepted"):
        await respond(db, config, request_id, accept=False)


async def test_only_contacts_or_managers_can_respond(db, config, servers):
    request_id = await request(db, config)
    with pytest.raises(PermissionDenied):
        await respond(db, config, request_id, accept=True, responder=424242)
    # a current manager (checked live in Discord by the caller) may answer
    result = await respond(db, config, request_id, accept=True, responder=424242, manager=True)
    assert result.status == RequestStatus.ACCEPTED


async def test_decline_applies_cooldown_then_allows_again(db, config, servers):
    request_id = await request(db, config)
    declined = await respond(db, config, request_id, accept=False)
    assert declined.status == RequestStatus.DECLINED

    soon = NOW + timedelta(hours=1)
    with pytest.raises(CooldownActive, match="declined recently"):
        await request(db, config, now=soon)

    after = NOW + timedelta(hours=config.partnerships.decline_cooldown_hours, seconds=1)
    new_id = await request(db, config, now=after)
    assert new_id != request_id


async def test_max_pending_requests(db, config, servers):
    from dataclasses import replace

    config = replace(
        config,
        partnerships=replace(
            config.partnerships,
            max_pending_requests=1,
            request_cooldown_seconds=0,
            server_request_cooldown_seconds=0,
        ),
    )
    await request(db, config)
    with pytest.raises(ValidationError, match="pending requests"):
        await request(db, config, target=OTHER)


async def test_old_requests_expire(db, config, servers):
    request_id = await request(db, config)
    async with db.session() as session:
        expired = await partnerships.expire_requests(
            session, config, NOW + timedelta(days=config.partnerships.request_expiration_days, minutes=1)
        )
        assert [r.id for r in expired] == [request_id]

async def test_duplicate_pending_message_names_both_servers_and_explains_other_admin(db, config, servers):
    async with db.session() as session:
        source = await repository.get_guild(session, SOURCE)
        target = await repository.get_guild(session, TARGET)
        source.name = "Rivals HQ"
        target.name = "Night Owls"

    await request(db, config)
    with pytest.raises(Conflict) as caught:
        await request(db, config, requester=7777, now=NOW + timedelta(minutes=10))

    message = str(caught.value)
    assert "**Rivals HQ**" in message
    assert "**Night Owls**" in message
    assert "Another admin may have sent it already" in message
    assert "Partnership Requests" in message

async def test_partner_board_post_cooldown_is_per_server(db, config, servers):
    async with db.session() as session:
        await partnerships.claim_looking_post(
            session, config,
            source_guild_id=SOURCE, actor_id=REQUESTER, message="Looking for gaming servers", now=NOW,
        )

    with pytest.raises(CooldownActive, match="can post again"):
        async with db.session() as session:
            await partnerships.claim_looking_post(
                session, config,
                source_guild_id=SOURCE, actor_id=7777, message="Another partner post",
                now=NOW + timedelta(minutes=1),
            )

    later = NOW + timedelta(minutes=config.partnerships.looking_post_cooldown_minutes, seconds=1)
    async with db.session() as session:
        await partnerships.claim_looking_post(
            session, config,
            source_guild_id=SOURCE, actor_id=7777, message="Fresh partner post", now=later,
        )

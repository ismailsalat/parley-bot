"""“Verify My Servers”: proving which servers someone manages, without the bot.

Listing a server must not require installing Parley, so Parley cannot read
Manage Server from the gateway. Instead the user authorises a read-only OAuth2
login (``identify`` + ``guilds``) and Discord tells us which guilds they manage.

Security rules, all enforced here:

* the state is cryptographically random, short-lived and single use
* the state is bound to the Discord user who pressed the button, and the
  authorising Discord account must be that same user
* only guilds where that account is owner / Administrator / Manage Server
  are eligible, read from Discord's own permission bits
* a guild id is never taken from the user: it must be in the verified set

Access tokens are used inside the callback and never stored.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import discord
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import OAuthSession
from bot.services.errors import ValidationError

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
# Read-only: who you are, and which servers you are in. Never `bot`.
VERIFY_SCOPES = ("identify", "guilds")
SESSION_TTL = timedelta(minutes=10)
# How long a completed verification can be used to start a listing.
VERIFIED_TTL = timedelta(minutes=30)

STATE_EXPIRED = "That verification link expired. Press **Verify My Servers** again."
STATE_UNKNOWN = "That verification link is no longer valid. Press **Verify My Servers** again."
WRONG_ACCOUNT = "That Discord account isn't the one that started the verification."


class VerificationError(ValidationError):
    """Anything that makes a verification attempt untrustworthy."""


@dataclass(frozen=True)
class VerifiedGuild:
    """A server the verified account manages, as Discord reported it."""

    id: int
    name: str
    icon_url: str | None = None
    member_count: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> VerifiedGuild:
        guild_id = int(payload["id"])
        icon = payload.get("icon")
        return cls(
            id=guild_id,
            name=str(payload.get("name") or f"Server {guild_id}"),
            icon_url=f"https://cdn.discordapp.com/icons/{guild_id}/{icon}.png" if icon else None,
            # only present when the guild list is fetched with counts
            member_count=int(payload.get("approximate_member_count") or 0),
        )

    def to_row(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "icon_url": self.icon_url, "member_count": self.member_count}


def manages(payload: dict[str, Any]) -> bool:
    """Owner, Administrator or Manage Server, using Discord's own permission bits."""
    if payload.get("owner"):
        return True
    try:
        permissions = discord.Permissions(int(payload.get("permissions", 0)))
    except (TypeError, ValueError):
        return False
    return permissions.administrator or permissions.manage_guild


def eligible_guilds(payloads: list[dict[str, Any]]) -> list[VerifiedGuild]:
    guilds = [VerifiedGuild.from_payload(p) for p in payloads if manages(p)]
    return sorted(guilds, key=lambda g: g.name.lower())


def new_state() -> str:
    return secrets.token_urlsafe(32)


def authorize_url(*, client_id: int, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": str(client_id),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(VERIFY_SCOPES),
            "state": state,
            "prompt": "consent",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


async def start(session: AsyncSession, *, user_id: int, now: datetime, ttl: timedelta = SESSION_TTL) -> OAuthSession:
    """Open a verification session for the user who pressed the button."""
    await session.execute(delete(OAuthSession).where(OAuthSession.expires_at < now - VERIFIED_TTL))
    row = OAuthSession(
        state=new_state(), discord_user_id=user_id, created_at=now, expires_at=now + ttl
    )
    session.add(row)
    await session.flush()
    log.info("verify.started user_id=%s", user_id)
    return row


async def _claim(session: AsyncSession, state: str, now: datetime) -> OAuthSession:
    """Spend the state, exactly once.

    The claim is a single conditional UPDATE, so two callbacks arriving at the
    same moment cannot both win: the database decides. Reading first and then
    writing would let both pass the check before either wrote.
    """
    claimed = await session.execute(
        update(OAuthSession)
        .where(OAuthSession.state == state, OAuthSession.used_at.is_(None), OAuthSession.expires_at > now)
        .values(used_at=now)
        .returning(OAuthSession.id)
    )
    won = claimed.scalar_one_or_none()
    # Commit the burn now: if what follows fails (wrong account, bad payload) the
    # surrounding session rolls back, and the state must not become usable again.
    await session.commit()

    row = await session.scalar(select(OAuthSession).where(OAuthSession.state == state))
    if won is not None:
        assert row is not None
        return row
    # Lost the race, or the link was already spent, unknown or expired.
    if row is not None and row.used_at is None and row.expires_at <= now:
        raise VerificationError(STATE_EXPIRED)
    raise VerificationError(STATE_UNKNOWN)


async def complete(
    session: AsyncSession, *, state: str, oauth_user_id: int, payloads: list[dict[str, Any]], now: datetime
) -> tuple[OAuthSession, list[VerifiedGuild]]:
    """Finish a verification: the state is spent here, whatever the outcome."""
    row = await _claim(session, state, now)
    if oauth_user_id != row.discord_user_id:
        # Someone else authorised with this link: burn it and refuse.
        log.warning("verify.identity_mismatch expected=%s got=%s", row.discord_user_id, oauth_user_id)
        raise VerificationError(WRONG_ACCOUNT)
    guilds = eligible_guilds(payloads)
    row.guilds = [g.to_row() for g in guilds]
    row.completed_at = now
    log.info("verify.completed user_id=%s guilds=%d", row.discord_user_id, len(guilds))
    return row, guilds


@dataclass(frozen=True)
class VerificationResult:
    """What Parley knows about this user's most recent verification.

    ``completed`` separates "the browser step hasn't finished" from "it finished
    and Discord reported no server you manage" - two very different answers.
    """

    completed: bool
    guilds: list[VerifiedGuild]

    def __bool__(self) -> bool:
        return bool(self.guilds)


async def latest_verification(session: AsyncSession, *, user_id: int, now: datetime) -> VerificationResult:
    """The most recent completed verification for this user, if it is still fresh."""
    row = await session.scalar(
        select(OAuthSession)
        .where(
            OAuthSession.discord_user_id == user_id,
            OAuthSession.completed_at.is_not(None),
            OAuthSession.completed_at > now - VERIFIED_TTL,
        )
        .order_by(OAuthSession.completed_at.desc())
        .limit(1)
    )
    if row is None:
        return VerificationResult(completed=False, guilds=[])
    return VerificationResult(completed=True, guilds=[VerifiedGuild(**p) for p in (row.guilds or [])])


async def verified_guilds(session: AsyncSession, *, user_id: int, now: datetime) -> list[VerifiedGuild]:
    """Just the servers, for callers that don't care why the list is empty."""
    return (await latest_verification(session, user_id=user_id, now=now)).guilds


async def require_verified_guild(
    session: AsyncSession, *, user_id: int, guild_id: int, now: datetime
) -> VerifiedGuild:
    """A guild id is only ever accepted because Discord vouched for it."""
    for guild in await verified_guilds(session, user_id=user_id, now=now):
        if guild.id == guild_id:
            return guild
    raise VerificationError("That server is no longer verified. Press **Verify My Servers** again.")

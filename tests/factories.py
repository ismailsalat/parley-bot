"""Small builders shared by the tests."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.runtime import RuntimeConfig
from bot.database.models import Listing
from bot.services import listings as listing_service
from bot.services.listings import GuildInfo, ListingInput

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
OWNER = 1000


def guild(guild_id: int, name: str | None = None, members: int = 500) -> GuildInfo:
    return GuildInfo(guild_id=guild_id, name=name or f"Server {guild_id}", icon_url=None, member_count=members)


def listing_input(**overrides) -> ListingInput:
    values = dict(
        categories=["Gaming"],
        accepting_partnerships=True,
        minimum_members=0,
        contact_ids=[],
        advertisement_text="## Welcome\n**Great** server",
        invite_url="https://discord.gg/abc123",
    )
    values.update(overrides)
    return ListingInput(**values)


async def make_listing(
    session: AsyncSession, config: RuntimeConfig, guild_id: int, *, actor_id: int = OWNER, members: int = 500, now=NOW, **fields
) -> Listing:
    return await listing_service.create_listing(
        session, config, guild=guild(guild_id, members=members), actor_id=actor_id, data=listing_input(**fields), now=now
    )

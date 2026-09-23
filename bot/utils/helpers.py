"""Small, dependency-free helpers."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_duration(delta: timedelta) -> str:
    """Human friendly, rounded *up* so users never retry too early.

    >>> format_duration(timedelta(minutes=36, seconds=10))
    '37 minutes'
    """
    seconds = max(0, math.ceil(delta.total_seconds()))
    if seconds < 60:
        return "1 minute" if seconds > 0 else "a moment"
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        text = f"{hours} hour{'s' if hours != 1 else ''}"
        return text + (f" {minutes} minute{'s' if minutes != 1 else ''}" if minutes else "")
    days, hours = divmod(hours, 24)
    text = f"{days} day{'s' if days != 1 else ''}"
    return text + (f" {hours} hour{'s' if hours != 1 else ''}" if hours else "")


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def parse_snowflake(raw: str) -> int | None:
    """Parse a Discord ID typed by staff. Returns None when invalid."""
    raw = raw.strip()
    if not raw.isdigit() or not 15 <= len(raw) <= 21:
        return None
    return int(raw)


def format_members(count: int | None) -> str:
    count = count or 0
    return f"{count:,} member{'s' if count != 1 else ''}"


def format_minimum(minimum: int) -> str:
    return "Any server size" if minimum <= 0 else f"{minimum:,}+ members"


def listing_jump_url(hub_guild_id: int, channel_id: int | None, message_id: int | None) -> str | None:
    """Return a jump link to the actual advertisement message, never its helper card."""
    if not hub_guild_id or not channel_id or not message_id:
        return None
    return f"https://discord.com/channels/{hub_guild_id}/{channel_id}/{message_id}"

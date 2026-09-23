"""Mention suppression: ads may *show* @everyone but must never ping."""

from __future__ import annotations

import discord

from bot.config.runtime import default_config
from bot.database.models import Listing
from bot.utils.mentions import advertisement_kwargs, safe_allowed_mentions

AD = "## Big news @everyone @here <@&123> <@456>\n**bold** `code`"


def test_safe_allowed_mentions_blocks_everything():
    mentions = safe_allowed_mentions()
    data = mentions.to_dict()
    assert data["parse"] == []  # no @everyone/@here, no roles, no users
    assert "users" not in data and "roles" not in data
    assert mentions.replied_user is False


def test_advertisement_is_plain_content_not_an_embed():
    kwargs = advertisement_kwargs(AD)
    assert kwargs["content"] == AD
    assert "embed" not in kwargs and "embeds" not in kwargs
    assert kwargs["allowed_mentions"].to_dict()["parse"] == []


class _Bot:
    runtime = default_config()


async def test_listing_message_uses_safe_mentions_and_buttons():
    from bot.views.partnership import listing_message_kwargs

    listing = Listing(guild_id=1, advertisement_text=AD, accepting_partnerships=True, invite_url="https://discord.gg/x")
    kwargs = listing_message_kwargs(_Bot(), listing)
    assert kwargs["content"] == AD
    assert kwargs["allowed_mentions"].to_dict()["parse"] == []
    labels = [getattr(child, "item", child).label for child in kwargs["view"].children]
    assert labels == ["Join Server", "Request Partnership"]


async def test_client_wide_default_is_also_safe(tmp_path):
    from bot.config.settings import Settings
    from bot.core import WaypointBot
    from bot.database.session import Database

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    bot = WaypointBot(Settings(discord_token="", database_url=db.url), db)
    try:
        assert isinstance(bot.allowed_mentions, discord.AllowedMentions)
        assert bot.allowed_mentions.to_dict()["parse"] == []
    finally:
        await bot.close()
        await db.dispose()

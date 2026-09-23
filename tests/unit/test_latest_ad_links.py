from __future__ import annotations

from bot.utils.helpers import listing_jump_url


def test_listing_jump_url_points_to_actual_ad_message():
    assert listing_jump_url(1, 2, 3) == "https://discord.com/channels/1/2/3"


def test_listing_jump_url_requires_all_ids():
    assert listing_jump_url(0, 2, 3) is None
    assert listing_jump_url(1, None, 3) is None
    assert listing_jump_url(1, 2, None) is None

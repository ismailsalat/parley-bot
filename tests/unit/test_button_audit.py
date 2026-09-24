"""Static audit: every button, action, command and custom_id is wired to a real handler."""

from __future__ import annotations

import re
from pathlib import Path

import discord

from bot.config.runtime import CUSTOMIZABLE_BUTTONS, DEFAULT_BUTTONS
from bot.core import persistent_items
from bot.views.welcome import registered_actions

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "bot").rglob("*.py"))


def test_every_action_button_used_in_code_has_a_handler():
    persistent_items()  # imports every view module
    used = set(re.findall(r'action_button\(\s*(?:self\.)?bot,\s*"([a-z_]+)"', SOURCE))
    used |= set(re.findall(r'ActionButton\(\s*"([a-z_]+)"', SOURCE))
    assert used, "audit found no buttons"
    missing = used - registered_actions()
    assert not missing, f"buttons without handlers: {missing}"


def test_every_handler_is_reachable_from_a_button():
    persistent_items()
    used = set(re.findall(r'"([a-z_]+)"', SOURCE))
    assert registered_actions() <= used


def test_custom_id_templates_are_unique_and_distinct():
    patterns = [cls.__discord_ui_compiled_template__.pattern for cls in persistent_items()]
    assert len(patterns) == len(set(patterns))
    prefixes = [p.split("(")[0] for p in patterns]
    assert len(prefixes) == len(set(prefixes)), prefixes


def test_every_customizable_button_has_a_default():
    assert set(CUSTOMIZABLE_BUTTONS) <= set(DEFAULT_BUTTONS)
    for key, spec in DEFAULT_BUTTONS.items():
        assert 1 <= len(spec["label"]) <= 40, key


async def test_no_duplicate_slash_commands(db):
    from bot.core import ParleyBot
    from tests.conftest import make_settings

    bot = ParleyBot(make_settings(db.url, main_guild_id=5, sync_commands=False), db)
    try:
        await bot.setup_hook()
        global_names = [c.name for c in bot.tree.get_commands()]
        staff_names = [c.name for c in bot.tree.get_commands(guild=discord.Object(id=5))]
        assert len(global_names) == len(set(global_names))
        assert not set(global_names) & set(staff_names)
        for command in bot.tree.walk_commands():
            assert command.description, command.qualified_name
    finally:
        await bot.close()


def test_slow_user_buttons_acknowledge_before_database_work():
    """The flows users hit most often must acknowledge Discord before slow DB/API work."""
    checks = {
        "bot/views/verify.py": ["async def _continue", "await acknowledge(interaction)"],
        "bot/views/partnership.py": ["class GuildPickerView", "await acknowledge(interaction)"],
        "bot/views/network.py": ["async def _save", "await acknowledge(interaction)"],
        "bot/views/listings.py": ["async def _verify_another", "await acknowledge(interaction)"],
    }
    for rel, needles in checks.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        for needle in needles:
            assert needle in text, f"{rel} is missing {needle!r}"


def test_startup_force_refreshes_persistent_panels():
    core = (ROOT / "bot" / "core.py").read_text(encoding="utf-8")
    assert "await self.panels.restore_panels(force_edit=True)" in core

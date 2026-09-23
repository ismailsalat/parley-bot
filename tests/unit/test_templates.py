"""Message templates: documented placeholders only, safe rendering."""

from __future__ import annotations

from dataclasses import replace

import pytest

from bot.config import templates
from bot.config.runtime import default_config


def test_every_template_renders_with_defaults():
    config = default_config()
    for key in templates.TEMPLATES:
        text = templates.render(config, key, server_name="S", requester_server="A", target_server="B", jump_url="u")
        assert "{" not in text.replace("{{", "").replace("}}", ""), key


def test_placeholders_are_filled():
    config = default_config()
    text = templates.render(config, "request_declined", requester_server="Cats", target_server="Dogs")
    assert "Cats" in text and "Dogs" in text


@pytest.mark.parametrize(
    ("key", "text", "error"),
    [
        ("dm_home", "Hi {server_name}", "Unknown placeholder"),  # not available in this template
        ("welcome", "{bot_name.__class__}", "valid placeholder"),
        ("welcome", "{bot_name!r}", "formatting"),
        ("welcome", "{bot_name:>50}", "formatting"),
        ("welcome", "oops {", "braces"),
        ("welcome", "   ", "empty"),
        ("welcome", "x" * 2000, "too long"),
    ],
)
def test_invalid_templates_are_rejected(key, text, error):
    with pytest.raises(templates.TemplateError, match=error):
        templates.validate(key, text)


def test_literal_braces_and_empty_footer_are_allowed():
    assert templates.validate("welcome", "Use {{this}} {bot_name}") == "Use {{this}} {bot_name}"
    assert templates.validate("network_footer", "") == ""


def test_broken_stored_template_falls_back_to_default():
    config = default_config()
    broken = replace(config, messages=replace(config.messages, dm_home="Hello {nope}"))
    assert templates.render(broken, "dm_home") == templates.render(config, "dm_home")


def test_bot_name_placeholder_follows_settings():
    config = default_config()
    renamed = replace(config, bot=replace(config.bot, name="Nexus"))
    assert templates.render(renamed, "maintenance").startswith("Nexus")

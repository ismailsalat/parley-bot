"""One install path.

Parley is a **guild install**: it is added to a server and keeps a bot user there,
because the Connected perks need it inside the server. These tests pin the single
install link, its scopes and permissions, and stop a second (or hand-typed)
install URL creeping back into the product.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from bot.config.runtime import default_config
from bot.views.welcome import (
    INSTALL_SCOPES,
    PUBLIC_PERMISSIONS,
    SETUP_PERMISSIONS,
    add_bot_button,
    invite_url,
    setup_invite_url,
)

ROOT = Path(__file__).resolve().parents[2]
APP_ID = 123456789012345678


class InstallBot:
    def __init__(self, application_id: int | None = APP_ID) -> None:
        self.runtime = default_config()
        self.application_id = application_id


def query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def test_install_link_is_a_guild_install_authorize_url():
    url = invite_url(InstallBot())
    parsed = urlparse(url)
    assert parsed.scheme == "https" and parsed.netloc == "discord.com"
    assert parsed.path == "/oauth2/authorize"
    fields = query(url)
    assert fields["client_id"] == [str(APP_ID)]
    # `bot` is the scope that makes Discord install the app into the server itself.
    assert set(fields["scope"][0].split()) == {"bot", "applications.commands"}


def test_install_scopes_are_declared_once():
    assert set(INSTALL_SCOPES) == {"bot", "applications.commands"}
    source = (ROOT / "bot" / "views" / "welcome.py").read_text(encoding="utf-8")
    assert "scopes=INSTALL_SCOPES" in source  # the builder uses the constant, not a literal


def test_each_link_asks_for_its_own_permissions():
    assert int(query(invite_url(InstallBot()))["permissions"][0]) == PUBLIC_PERMISSIONS.value
    assert int(query(setup_invite_url(InstallBot()))["permissions"][0]) == SETUP_PERMISSIONS.value
    # The hub needs more (Automatic Setup, the posting window), but never Administrator.
    assert SETUP_PERMISSIONS.value > PUBLIC_PERMISSIONS.value
    assert not PUBLIC_PERMISSIONS.administrator and not SETUP_PERMISSIONS.administrator
    for needed in ("view_channel", "send_messages", "read_message_history", "embed_links"):
        assert getattr(PUBLIC_PERMISSIONS, needed) is True


def test_no_link_before_login_instead_of_a_broken_one():
    assert invite_url(InstallBot(application_id=None)) is None
    assert add_bot_button(InstallBot(application_id=None)) is None


def test_every_add_parley_button_uses_the_canonical_builder():
    bot = InstallBot()
    assert add_bot_button(bot).url == invite_url(bot)


def test_only_one_place_builds_an_install_url():
    """A second *install* builder is how legacy links end up in old messages.

    bot/services/verification.py also builds an authorize URL, but for the
    read-only "Verify My Servers" login - a different purpose, asserted below.
    """
    builders = []
    for path in (ROOT / "bot").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "oauth_url(" in text or re.search(r"discord\.com/(?:api/)?oauth2/authorize", text):
            builders.append(path.relative_to(ROOT).as_posix())
    assert sorted(builders) == ["bot/services/verification.py", "bot/views/welcome.py"], builders


def test_verification_login_never_asks_for_the_bot_scope():
    """Verifying who you are must not install anything."""
    from bot.services import verification

    assert set(verification.VERIFY_SCOPES) == {"identify", "guilds"}
    url = verification.authorize_url(client_id=APP_ID, redirect_uri="https://example.com/cb", state="abc")
    scopes = set(query(url)["scope"][0].split())
    assert scopes == {"identify", "guilds"}
    assert "bot" not in scopes and "applications.commands" not in scopes
    assert query(url)["state"] == ["abc"]
    # and the install link still does ask for them
    assert set(query(invite_url(InstallBot()))["scope"][0].split()) == {"bot", "applications.commands"}


@pytest.mark.parametrize(
    ("module", "callback", "dms"),
    [
        ("bot.commands.find", "find", True),
        ("bot.commands.manage", "manage", True),
        ("bot.commands.help", "help", True),
        ("bot.commands.connect", "connect", False),
        ("bot.commands.network", "network", False),
        ("bot.commands.setup", "setup_command", False),
    ],
)
def test_commands_declare_a_guild_install(module, callback, dms):
    """Parley is a server app: installable to a guild, never to a user account."""
    namespace = importlib.import_module(module).__dict__
    cog = next(v for k, v in namespace.items() if k.endswith("Commands") and hasattr(v, "__cog_app_commands__"))
    command = next(c for c in cog.__cog_app_commands__ if c.callback.__name__ == callback)

    installs, contexts = command.allowed_installs, command.allowed_contexts
    assert installs is not None and installs.guild is True and installs.user is False
    assert contexts is not None and contexts.guild is True
    assert contexts.private_channel is False  # group DMs are a user-install surface
    # DM access must match what the command already supported.
    assert contexts.dm_channel is dms


def test_server_only_commands_are_still_guild_only():
    """The install contexts must not become the only thing stopping DM use."""
    for module, callback in (("bot.commands.connect", "connect"), ("bot.commands.network", "network"),
                             ("bot.commands.setup", "setup_command")):
        namespace = importlib.import_module(module).__dict__
        cog = next(v for k, v in namespace.items() if k.endswith("Commands") and hasattr(v, "__cog_app_commands__"))
        command = next(c for c in cog.__cog_app_commands__ if c.callback.__name__ == callback)
        assert command.guild_only is True

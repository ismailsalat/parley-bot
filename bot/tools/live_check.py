"""Live check against real Discord, using a separate TEST bot.

    set WAYPOINT_LIVE_TOKEN=...              (a test bot's token, never production)
    set WAYPOINT_TEST_GUILD_ID=...           (optional: a server the test bot is in)
    set WAYPOINT_TEST_CHANNEL_ID=...         (optional: a channel it may post in)
    python -m bot.tools.live_check

It verifies: login, intents, registered slash commands, DM capability (to the
bot owner), guild access, channel permissions, invite creation, sending and
deleting a message, and that every persistent button type loads. Everything it
creates is deleted again. It never touches the Waypoint database.
"""

from __future__ import annotations

import asyncio
import os
import sys

import discord

from bot.core import build_intents, persistent_items

OK, WARN, FAIL = "✅", "⚠️", "❌"


def invalid_built_in_emoji() -> list[str]:
    from bot.config.runtime import DEFAULT_BUTTONS
    from bot.utils.emoji import is_valid_emoji

    return [f"{key}={spec['emoji']}" for key, spec in DEFAULT_BUTTONS.items()
            if spec.get("emoji") and not is_valid_emoji(str(spec["emoji"]))]


def real_views() -> dict[str, discord.ui.View]:
    """The views a user actually sees, built exactly as Waypoint builds them."""
    from bot.config.runtime import default_config
    from bot.database.models import Listing
    from bot.views.partnership import listing_message_kwargs
    from bot.views.welcome import control_panel, listings_panel, looking_panel, welcome_panel

    class _Bot:
        runtime = default_config()
        application_id = None

    bot = _Bot()
    listing = Listing(guild_id=1, advertisement_text="x", accepting_partnerships=True, invite_url="https://discord.gg/x")
    return {
        "DM home": control_panel(bot)[1],
        "listings panel": listings_panel(bot)[1],
        "looking panel": looking_panel(bot)[1],
        "welcome panel": welcome_panel(bot)[1],
        "listing buttons": listing_message_kwargs(bot, listing)["view"],
    }


class LiveCheck(discord.Client):
    def __init__(self, guild_id: int | None, channel_id: int | None) -> None:
        super().__init__(intents=build_intents())
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.results: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.results.append((status, name, detail))

    async def on_ready(self) -> None:
        try:
            await self.run_checks()
        finally:
            await self.close()

    async def run_checks(self) -> None:
        assert self.user is not None
        self.add(OK, "Login", f"{self.user} · members intent accepted")
        self.add(OK, "Persistent buttons", f"{len(persistent_items())} types load")

        bad = invalid_built_in_emoji()
        self.add(*( (FAIL, "Button emoji", f"Discord will reject: {', '.join(bad)}") if bad
                    else (OK, "Button emoji", "every built-in emoji is valid") ))

        app = await self.application_info()
        commands = await self.http.get_global_commands(app.id)
        names = sorted(c["name"] for c in commands)
        self.add(OK if names else WARN, "Global slash commands", ", ".join(names) or "none registered yet (start Waypoint once)")

        owner = app.team.owner if app.team and app.team.owner else app.owner
        try:
            message = await owner.send("🧪 Waypoint live check: DMs work. (This message deletes itself.)")
            await message.delete()
            self.add(OK, "DM to owner")
        except discord.HTTPException as exc:
            self.add(WARN, "DM to owner", f"failed: {exc.text or exc}")

        if self.guild_id is None:
            self.add(WARN, "Test server", "set WAYPOINT_TEST_GUILD_ID to check permissions and posting")
            return
        guild = self.get_guild(self.guild_id)
        if guild is None:
            self.add(FAIL, "Test server", "the test bot isn't in that server")
            return
        self.add(OK, "Test server", guild.name)
        perms = guild.me.guild_permissions
        for name in ("view_channel", "send_messages", "embed_links", "read_message_history", "create_instant_invite", "manage_channels"):
            self.add(OK if getattr(perms, name) else WARN, f"Permission: {name}")

        channel = guild.get_channel(self.channel_id) if self.channel_id else None
        if not isinstance(channel, discord.TextChannel):
            self.add(WARN, "Test channel", "set WAYPOINT_TEST_CHANNEL_ID to test posting and invites")
            return
        try:
            message = await channel.send("🧪 Waypoint live check (deleting)", allowed_mentions=discord.AllowedMentions.none())
            await message.delete()
            self.add(OK, "Send + delete message", f"#{channel.name}")
        except discord.HTTPException as exc:
            self.add(FAIL, "Send + delete message", str(exc))

        # Render the real user-facing buttons: this is what catches emoji Discord refuses.
        for name, view in real_views().items():
            try:
                message = await channel.send(f"🧪 Waypoint live check · {name}", view=view,
                                             allowed_mentions=discord.AllowedMentions.none())
                await message.delete()
                self.add(OK, f"Buttons: {name}")
            except discord.HTTPException as exc:
                self.add(FAIL, f"Buttons: {name}", (exc.text or str(exc))[:200])
        try:
            invite = await channel.create_invite(max_age=60, max_uses=1, unique=True, reason="Waypoint live check")
            await invite.delete()
            self.add(OK, "Create invite")
        except discord.HTTPException as exc:
            self.add(WARN, "Create invite", str(exc))


def main() -> int:
    token = os.getenv("WAYPOINT_LIVE_TOKEN", "").strip()
    if not token:
        print("Set WAYPOINT_LIVE_TOKEN to a TEST bot's token (never your production token).", file=sys.stderr)
        return 2
    guild_id = int(os.getenv("WAYPOINT_TEST_GUILD_ID") or 0) or None
    channel_id = int(os.getenv("WAYPOINT_TEST_CHANNEL_ID") or 0) or None
    client = LiveCheck(guild_id, channel_id)
    try:
        asyncio.run(client.start(token))
    except discord.LoginFailure:
        print("❌ The test token was rejected.", file=sys.stderr)
        return 1
    except discord.PrivilegedIntentsRequired:
        print("❌ Enable SERVER MEMBERS INTENT for the test bot in the Developer Portal.", file=sys.stderr)
        return 1
    except (discord.HTTPException, OSError) as exc:
        print(f"❌ Could not reach Discord: {exc}", file=sys.stderr)
        return 1
    for status, name, detail in client.results:
        print(f"{status} {name}" + (f" — {detail}" if detail else ""))
    return 1 if any(status == FAIL for status, _, _ in client.results) else 0


if __name__ == "__main__":
    sys.exit(main())

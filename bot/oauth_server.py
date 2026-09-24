"""The tiny HTTP surface Parley needs for “Verify My Servers”.

One callback route, served by aiohttp (already a discord.py dependency, so no
web framework is added). It exchanges the authorization code, asks Discord who
authorised and which guilds they manage, records the verified guilds against
the session, and shows a page telling the person to go back to Discord.

The access token lives inside this request and is never stored or logged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from bot.services import verification
from bot.services.errors import ParleyError
from bot.utils.helpers import utcnow

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)

TOKEN_URL = "https://discord.com/api/v10/oauth2/token"
API_BASE = "https://discord.com/api/v10"
CALLBACK_PATH = "/oauth/discord/callback"


def _page(title: str, message: str, *, ok: bool = True) -> web.Response:
    """A plain, dependency-free page. No user content is echoed back into it."""
    colour = "#3ba55c" if ok else "#ed4245"
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title></head>
<body style="font-family:system-ui,sans-serif;background:#313338;color:#f2f3f5;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
  <main style="max-width:28rem;padding:2rem;text-align:center">
    <h1 style="color:{colour};font-size:1.4rem;margin:0 0 .75rem">{title}</h1>
    <p style="line-height:1.5;margin:0">{message}</p>
  </main>
</body></html>"""
    return web.Response(text=html, content_type="text/html", status=200 if ok else 400)


async def exchange_code(session: aiohttp.ClientSession, bot: ParleyBot, code: str) -> str:
    """Swap the authorization code for a short-lived access token."""
    settings = bot.settings
    data = {
        "client_id": str(settings.oauth_client_id or bot.application_id),
        "client_secret": settings.oauth_client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.oauth_redirect_uri,
    }
    async with session.post(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}) as response:
        if response.status != 200:
            log.warning("oauth.token_exchange_failed status=%s", response.status)  # never log the body: it echoes the secret
            raise verification.VerificationError("Discord refused that verification. Please try again.")
        payload = await response.json()
    token = payload.get("access_token")
    if not token:
        raise verification.VerificationError("Discord refused that verification. Please try again.")
    return str(token)


async def fetch_identity(session: aiohttp.ClientSession, token: str) -> tuple[int, list[dict[str, Any]]]:
    """Who authorised, and which guilds they are in (with member counts)."""
    headers = {"Authorization": f"Bearer {token}"}
    async with session.get(f"{API_BASE}/users/@me", headers=headers) as response:
        response.raise_for_status()
        user = await response.json()
    async with session.get(f"{API_BASE}/users/@me/guilds?with_counts=true", headers=headers) as response:
        response.raise_for_status()
        guilds = await response.json()
    return int(user["id"]), list(guilds)


class OAuthServer:
    """Runs alongside the bot; a no-op when verification isn't configured."""

    def __init__(self, bot: ParleyBot) -> None:
        self.bot = bot
        self._runner: web.AppRunner | None = None

    @property
    def enabled(self) -> bool:
        return self.bot.settings.oauth_enabled

    async def start(self) -> None:
        if not self.enabled or self._runner is not None:
            return
        app = web.Application()
        app.router.add_get(CALLBACK_PATH, self.handle_callback)
        app.router.add_get("/healthz", lambda _request: web.Response(text="ok"))
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.bot.settings.oauth_port)
        await site.start()
        log.info("Verification callback listening on port %s%s", self.bot.settings.oauth_port, CALLBACK_PATH)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def handle_callback(self, request: web.Request) -> web.Response:
        state = request.query.get("state", "")
        code = request.query.get("code", "")
        if not state or not code:
            # Includes the user pressing Cancel on Discord's consent screen.
            return _page("Verification cancelled", "Nothing was changed. You can close this tab.", ok=False)
        try:
            async with aiohttp.ClientSession() as session:
                token = await exchange_code(session, self.bot, code)
                oauth_user_id, payloads = await fetch_identity(session, token)
            async with self.bot.db.session() as db:
                _row, guilds = await verification.complete(
                    db, state=state, oauth_user_id=oauth_user_id, payloads=payloads, now=utcnow()
                )
        except ParleyError as exc:
            return _page("Verification failed", exc.user_message, ok=False)
        except Exception:  # noqa: BLE001 - the browser gets a plain page, the detail goes to the log
            log.exception("oauth.callback_failed")
            return _page("Verification failed", "Something went wrong. Please try again from Discord.", ok=False)

        found = f"Parley found {len(guilds)} server(s) you manage." if guilds else (
            "Parley didn't find a server where you have Manage Server."
        )
        return _page("You're verified", f"{found}<br><br>Go back to Discord and press <b>Continue</b>.")

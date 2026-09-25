"""The tiny HTTP surface Parley needs for “Verify My Servers”.

The callback exchanges the authorization code, records the servers the user
manages, updates the Discord verification message when possible, then renders a
small branded success page. Access tokens live only inside the callback request
and are never stored or logged.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from bot.services import verification
from bot.services.errors import ParleyError
from bot.utils.helpers import utcnow

if TYPE_CHECKING:
    import discord

    from bot.core import ParleyBot

log = logging.getLogger(__name__)

TOKEN_URL = "https://discord.com/api/v10/oauth2/token"
API_BASE = "https://discord.com/api/v10"
CALLBACK_PATH = "/oauth/discord/callback"
_PENDING_TTL = timedelta(minutes=12)

@dataclass
class PendingDiscordRefresh:
    interaction: discord.Interaction
    user_id: int
    expires_at: datetime


def _page(title: str, message: str, *, hint: str | None = None, ok: bool = True) -> web.Response:
    """Render a polished, dependency-free status page.

    All dynamic text is escaped. The animation and visual treatment are pure
    CSS, so there are no third-party scripts, trackers, or external assets.
    """
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    safe_hint = html.escape(hint or "")
    accent = "#3ddc97" if ok else "#ff6b6b"
    soft = "rgba(61, 220, 151, .18)" if ok else "rgba(255, 107, 107, .16)"
    icon = (
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5 9.2 17 19 7"/></svg>'
        if ok
        else '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 8l8 8M16 8l-8 8"/></svg>'
    )
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#0b1020">
  <title>Parley · {safe_title}</title>
  <style>
    :root {{ color-scheme: dark; --accent:{accent}; --soft:{soft}; }}
    * {{ box-sizing:border-box; }}
    html, body {{ min-height:100%; }}
    body {{
      margin:0;
      min-height:100vh;
      display:grid;
      place-items:center;
      overflow:hidden;
      font-family:Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color:#f6f8fb;
      background:
        radial-gradient(circle at 18% 16%, rgba(64, 88, 150, .18), transparent 34%),
        radial-gradient(circle at 84% 82%, var(--soft), transparent 34%),
        linear-gradient(145deg, #0a0f1d 0%, #101727 48%, #0a0f1a 100%);
    }}
    body::before, body::after {{
      content:"";
      position:fixed;
      width:28rem;
      height:28rem;
      border-radius:999px;
      filter:blur(70px);
      opacity:.16;
      pointer-events:none;
      animation:drift 10s ease-in-out infinite alternate;
    }}
    body::before {{ left:-13rem; bottom:-15rem; background:#5865f2; }}
    body::after {{ right:-14rem; top:-15rem; background:var(--accent); animation-delay:-4s; }}
    .card {{
      width:min(92vw, 34rem);
      padding:2.15rem 2rem 2rem;
      text-align:center;
      border:1px solid rgba(255,255,255,.11);
      border-radius:24px;
      background:linear-gradient(160deg, rgba(29,38,57,.88), rgba(14,20,33,.92));
      box-shadow:0 24px 70px rgba(0,0,0,.42), 0 0 0 1px rgba(255,255,255,.02) inset;
      backdrop-filter:blur(18px);
      animation:rise .5s cubic-bezier(.2,.8,.2,1) both;
    }}
    .brand {{
      display:inline-flex;
      align-items:center;
      gap:.55rem;
      margin-bottom:1.65rem;
      color:#eef2f8;
      font-weight:750;
      font-size:1.08rem;
      letter-spacing:.01em;
    }}
    .brand-mark {{
      display:grid;
      place-items:center;
      width:2rem;
      height:2rem;
      border-radius:10px;
      background:rgba(255,255,255,.07);
      font-size:1.05rem;
    }}
    .status {{
      width:5.4rem;
      height:5.4rem;
      margin:0 auto 1.35rem;
      display:grid;
      place-items:center;
      border-radius:50%;
      color:white;
      background:linear-gradient(145deg, color-mix(in srgb, var(--accent) 86%, white), var(--accent));
      box-shadow:0 0 0 12px var(--soft), 0 14px 38px var(--soft);
      animation:pop .58s .08s cubic-bezier(.2,1.35,.5,1) both, breathe 2.8s 1s ease-in-out infinite;
    }}
    .status svg {{ width:2.7rem; height:2.7rem; fill:none; stroke:currentColor; stroke-width:2.4; stroke-linecap:round; stroke-linejoin:round; }}
    h1 {{
      margin:.15rem 0 .7rem;
      color:var(--accent);
      font-size:clamp(1.8rem, 5vw, 2.45rem);
      line-height:1.08;
      letter-spacing:-.035em;
    }}
    .message {{ margin:0; color:#d9e0ea; font-size:1.02rem; line-height:1.6; }}
    .hint {{
      margin:1.25rem auto 0;
      max-width:27rem;
      padding-top:1.1rem;
      border-top:1px solid rgba(255,255,255,.08);
      color:#9ea9b9;
      font-size:.92rem;
      line-height:1.5;
    }}
    .dot {{
      display:inline-block;
      width:.42rem;
      height:.42rem;
      margin-right:.45rem;
      border-radius:50%;
      vertical-align:.08rem;
      background:var(--accent);
      box-shadow:0 0 14px var(--accent);
    }}
    @keyframes rise {{ from {{ opacity:0; transform:translateY(14px) scale(.985); }} to {{ opacity:1; transform:none; }} }}
    @keyframes pop {{ from {{ opacity:0; transform:scale(.72); }} to {{ opacity:1; transform:scale(1); }} }}
    @keyframes breathe {{ 50% {{ box-shadow:0 0 0 16px var(--soft), 0 18px 42px var(--soft); transform:translateY(-2px); }} }}
    @keyframes drift {{ to {{ transform:translate3d(2.5rem, 1.5rem, 0) scale(1.08); }} }}
    @media (prefers-reduced-motion: reduce) {{ * {{ animation:none !important; }} }}
    @media (max-width:520px) {{ .card {{ padding:1.8rem 1.3rem 1.6rem; border-radius:20px; }} }}
  </style>
</head>
<body>
  <main class="card" role="status" aria-live="polite">
    <div class="brand"><span class="brand-mark">🤝</span><span>Parley</span></div>
    <div class="status">{icon}</div>
    <h1>{safe_title}</h1>
    <p class="message">{safe_message}</p>
    {f'<p class="hint"><span class="dot"></span>{safe_hint}</p>' if safe_hint else ''}
  </main>
</body>
</html>"""
    return web.Response(
        text=html_text,
        content_type="text/html",
        status=200 if ok else 400,
        headers={"Cache-Control": "no-store"},
    )


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
            log.warning("oauth.token_exchange_failed status=%s", response.status)
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
        self._pending: dict[str, PendingDiscordRefresh] = {}

    @property
    def enabled(self) -> bool:
        return self.bot.settings.oauth_enabled

    def watch(self, state: str, interaction: discord.Interaction) -> None:
        """Remember the live ephemeral message so OAuth can refresh it automatically."""
        now = utcnow()
        self._pending = {
            key: pending
            for key, pending in self._pending.items()
            if pending.expires_at > now and pending.user_id != interaction.user.id
        }
        self._pending[state] = PendingDiscordRefresh(
            interaction=interaction,
            user_id=interaction.user.id,
            expires_at=now + _PENDING_TTL,
        )

    def forget(self, state: str) -> None:
        self._pending.pop(state, None)

    async def refresh_discord(self, state: str, user_id: int) -> bool:
        """Replace the verification card with the server picker after OAuth succeeds."""
        pending = self._pending.pop(state, None)
        if pending is None or pending.user_id != user_id or pending.expires_at <= utcnow():
            return False
        try:
            from bot.views.verify import show_verified_servers

            await show_verified_servers(pending.interaction)
            return True
        except Exception:  # noqa: BLE001 - browser success must not fail because a Discord edit did
            log.exception("oauth.discord_auto_refresh_failed user_id=%s", user_id)
            return False

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
        self._pending.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def handle_callback(self, request: web.Request) -> web.Response:
        state = request.query.get("state", "")
        code = request.query.get("code", "")
        if not state or not code:
            if state:
                self.forget(state)
            return _page(
                "Verification cancelled",
                "Nothing changed.",
                hint="You can close this tab and return to Discord.",
                ok=False,
            )
        try:
            async with aiohttp.ClientSession() as session:
                token = await exchange_code(session, self.bot, code)
                oauth_user_id, payloads = await fetch_identity(session, token)
            async with self.bot.db.session() as db:
                _row, guilds = await verification.complete(
                    db, state=state, oauth_user_id=oauth_user_id, payloads=payloads, now=utcnow()
                )
        except ParleyError as exc:
            return _page(
                "Verification failed",
                exc.user_message,
                hint="Return to Discord and try again.",
                ok=False,
            )
        except Exception:  # noqa: BLE001 - the browser gets a friendly page, detail stays in logs
            log.exception("oauth.callback_failed")
            return _page(
                "Verification failed",
                "Something went wrong while talking to Discord.",
                hint="Return to Discord and try again.",
                ok=False,
            )

        updated = await self.refresh_discord(state, oauth_user_id)
        if len(guilds) == 1:
            found = "1 server is ready."
        elif guilds:
            found = f"{len(guilds)} servers are ready."
        else:
            found = "Your Discord account was verified."

        hint = (
            "Discord updated automatically. You can close this tab."
            if updated
            else "You're all set. Return to Discord and open Post Server Ad again."
        )
        return _page("You're verified", found, hint=hint)

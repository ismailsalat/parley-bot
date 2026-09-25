# PARLEY

**Connect once. Post once. Find partners. Talk to people.**

Parley is a Discord bot for server advertising and partnerships. Server owners list their server once, their ad appears in a shared listings channel, and other servers can find them and request a partnership with one button. Everything is buttons, menus and DMs.

You only ever put **two things** in a file: the bot token and (on Railway) the database. Everything else is set up inside Discord with **/setup** and **/settings**.

- [Quick start: Windows](#quick-start-windows)
- [Quick start: Railway](#quick-start-railway)
- [Create the Discord bot](#create-the-discord-bot)
- [Set up in Discord](#set-up-in-discord)
- [Everyday admin: /settings](#everyday-admin-settings)
- [How people use Parley](#how-people-use-waypoint)
- [Troubleshooting](#troubleshooting)
- [Advanced and recovery tools](#advanced-and-recovery-tools)

---

## Quick start: Windows

1. Download the ZIP and extract it. Double-click **`setup.bat`**.
2. Paste your bot token into `.env` after `DISCORD_TOKEN=` (setup offers to open it). [How to get a token](#create-the-discord-bot)
3. Double-click **`start.bat`**. Wait for `Parley is ready`.
4. In your Discord server, type **/setup** and press **✨ Automatic Setup**.

That's it. You never need to edit `.env` again.

You need Python 3.12 or newer from <https://www.python.org/downloads/>. Tick **"Add python.exe to PATH"** when installing.

## Quick start: Railway

1. Put the project on GitHub (private repository is fine). See [Put it on GitHub](#put-it-on-github).
2. On [railway.com](https://railway.com): **New Project → Deploy from GitHub repo**, then **+ Create → Database → PostgreSQL**.
3. Open the Parley service → **Variables** and add:
   - `DISCORD_TOKEN` = your bot token
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
4. Railway deploys automatically. Look for `Parley is ready` in the deployment logs.
5. In your Discord server, type **/setup**.

After that, all configuration happens in Discord. No more visits to Railway Variables.

---

## Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. **Bot** page:
   - **Reset Token** → **Copy**. This is your `DISCORD_TOKEN`. Never share it.
   - Under **Privileged Gateway Intents**, turn on **SERVER MEMBERS INTENT** and **MESSAGE CONTENT INTENT**. Leave Presence off.
3. Start Parley. It DMs you (the bot owner) a button that adds it to your main server with exactly the right permissions. You never have to build an invite link yourself.

If the DM doesn't arrive, use **OAuth2 → URL Generator** with scopes `bot` + `applications.commands` and the permissions below.

### Listing a server (no bot required)

Listing a server never requires installing Parley. **Post Server Ad** offers **Verify My Servers**:
a read-only Discord login (`identify` + `guilds`) that tells Parley which servers you manage. You
pick one, paste an invite, and the listing goes live with Parley in none of your servers.

Adding Parley stays optional and unlocks the Connected perks (faster Relists, Find Partners,
Partner Board posts, the Parley Network, Auto Partner, in-server partnership controls, live metadata).

To turn verification on, set these (the secret belongs in Railway variables, never in git):

| Variable | Value |
|---|---|
| `DISCORD_CLIENT_ID` | your application id |
| `DISCORD_CLIENT_SECRET` | Developer Portal → OAuth2 → Client Secret |
| `DISCORD_OAUTH_REDIRECT_URI` | `https://<your Railway domain>/oauth/discord/callback` |

Add that same URL under **Developer Portal → OAuth2 → Redirects**. Parley serves the callback
itself on `$PORT` with aiohttp (already a discord.py dependency), so Railway needs no extra
service. Without these three, verification stays off and the button explains that rather than
demanding the bot.

Verification sessions are single-use, expire in 10 minutes, are bound to the Discord account that
pressed the button, and no access token is ever stored.

### How Parley is installed

Parley is a **guild install**: it is added to a server and keeps a bot user there, because the
Connected perks (Find a Partner, Partner Posts, the Parley Network, Auto Partner, faster Relists,
live metadata, in-server partnership actions) need it inside the server. It is never a user install.

Every **Add Parley** button comes from one builder, `invite_url()` in `bot/views/welcome.py`, so
there is a single install link and no hand-typed or legacy URL:

```
https://discord.com/oauth2/authorize?client_id=<app id>&scope=bot+applications.commands&permissions=<set>
```

Match these in **Developer Portal → your app → Installation**, so the portal's own install link
agrees with the one Parley hands out:

| Setting | Value | Why |
|---|---|---|
| Installation Contexts | **Guild Install** ticked, **User Install** unticked | Parley manages servers; a user install has no bot user, so no Connected perk works |
| Install Link | **Discord Provided Link** | Gives the app an in-client **Add App** entry; Parley's own buttons keep working either way |
| Default Install Settings → Guild Install → Scopes | `bot`, `applications.commands` | `bot` performs the guild install; the other registers the slash commands |
| Default Install Settings → Guild Install → Permissions | the table below | Parley never asks for Administrator |

The commands declare the same policy in code (`allowed_installs` / `allowed_contexts`): `/connect`,
`/network` and `/setup` stay server-only, while `/find`, `/manage` and `/help` also work in a DM
with Parley, which the DM home relies on.

**Discord's "Add your first app" server checklist is Discord's own UI, not Parley's.** No API lets
an app tick it, and Parley does not fake it. See `AUDIT.md`.

**Permissions Parley asks for (never Administrator):**

| Permission | Why |
|---|---|
| View Channels, Send Messages, Read Message History | Post listings and panels, keep panels at the bottom |
| Embed Links | Request cards and search results |
| Create Instant Invite | Make an invite when a server owner doesn't paste one |
| Use External Emojis | Custom emoji in ads render correctly |
| Manage Channels *(main server only)* | Only for **Automatic Setup**. Without it, you pick existing channels instead. |

The **➕ Add Parley** button that other server owners see does not include Manage Channels.

**When Parley is added to another server:** it posts one small **Parley is connected** card with one main green action: **Find Partners**. That button is guided setup: if the server has no live ad, it opens the same synced ad wizard; when the ad is ready, it asks for the server's own partner-ad channel; then it opens partner discovery. **Post Server Ad** stays available separately for directory management. The server owner receives a one-time DM, and any server manager who actually uses Parley also receives one concise one-time DM for that server. Nothing is posted automatically just because the bot was invited.

---

## Set up in Discord

Type **/setup** in the server that should be your Parley hub. Only the owner of the bot application can do this, so no other server can claim to be the hub.

- **✨ Automatic Setup** creates five channels and posts the panels:
  - `#start-here`
  - `#server-directory`: members can read but not post; Parley manages it.
  - `#find-partners`
  - `#support`
  - `#waypoint-logs`: staff only.

  Channels that already exist are reused. Older untouched defaults (`#welcome`, `#partner-listings`, `#looking-for-partners`) are recognized; if Parley has Manage Channels, it renames those defaults to the clearer names above. Custom channel names are never changed.
- **⚙️ Choose Channels** lets you pick existing channels from menus and shows ✅/❌ for each permission Parley needs.
- **🧪 Test Setup** runs the Health Check.

A new installation starts in **🧪 TEST mode**:

- Only you and your staff can use Parley.
- Network ads are paused.
- Messages that would go to real users are held back, and you get a copy instead.
- Listings you post are marked 🧪.

Try everything, then press **🟢 Go Live**. The **Test Center → Test Utilities** page can reset test cooldowns, delete Test Center messages, or hard-clear only TEST listings so you can repeat the first-time setup flow quickly. Then you can delete the remaining test listings or keep them as real listings when you go live.

## Everyday admin: /settings

**/settings** in the main server, or DM Parley and press **⚙️ Parley Settings** (staff only).

| Page | What you can change |
|---|---|
The home has five choices; everything else is one level deeper.

| Menu | Inside |
|---|---|
| Server | **Channels** (pick channels, Repair Panels, Fix Permissions, import old `.env` values) and **Staff** roles |
| Appearance | **Buttons** (label, emoji, colour), **Messages**, **Links**, **Theme** |
| Rules | **Listings**, **Partnerships**, **Network**, **Categories** |
| Moderation | Search, suspended/banned servers, blocked users, approval queue |
| Tools | **Test**, **Health Check**, **Mode** (🧪 Test · 🟢 Live · 🔴 Off), **Export Settings** |

**Button colours follow one system:** blue for main actions, green only for approve/enable,
red only for destructive actions, and grey for navigation or low-priority controls such as Back, Home, Directory, and Relist. Emoji must be real Discord emoji —
a text symbol like `←` is refused with *"That isn't a valid Discord button emoji."*

Every change applies immediately, without a restart. Every section has **Reset to Defaults**, and it always asks first.

**Approval:** turn on *Approval required* to review new listings in `#waypoint-logs`. The review has Approve, Reject, View Server and Ban Server buttons. With *Re-approve ad edits* on (the default), an edited ad waits for staff while the old approved ad stays live. If the owner edits again, the earlier review is marked outdated and can't approve the newer text.

---

## How people use Parley

**Normal users only see this:**

- **DM Parley:** Post Server Ad · Find Partners · My Server Listings · My Partner Posts · Requests
- **My Listing:** a compact private embed with Relist · Edit · View Ad · Remove Listing. **View Ad** resolves the latest actual advertisement message, not Parley's helper card, then gives the user the real jump link.
- **Under every directory listing:** Join Server · Request Partnership
- **Partner Board:** only servers actively looking for partners appear here; posts use View Server · Request Partnership

If you manage several connected servers, **My Listing** becomes **My Servers** and uses one clean server picker. Server-choice screens use compact embeds; one-server users skip the picker entirely.

Partnership requests are also multi-server aware. If you can represent more than one listed server, Parley asks which server is sending the request, shows a clear **From → To** confirmation, and lets you change the source before sending. Duplicate requests name both servers so a second admin can immediately see that another admin may already have sent it. A configurable server-wide cooldown prevents two admins from firing requests too quickly.

**A server manager gets partnership-ready in one guided flow:**

Press **Find Partners**. Parley checks the prerequisites in order: a live synced server ad, then an enabled partner-ad channel, then partner discovery. The same ad and cooldowns are used whether setup starts from the connected server, DMs, or the main Parley hub.

**The server-ad wizard itself has three short steps:**

1. **Basics** — category and partnership status. If partnerships are open, choose the minimum partner size. Requests go to the person posting by default; contacts can be changed later under **Edit Server Info**.
2. **Your ad** — **Paste My Own Ad** opens a 3-minute window so you post the real message in `#server-directory`; **Simple Ad Builder** lets Parley write it for you.
3. Builder ads get a simple preview before **Publish**. Direct-post ads are already the real message, so there is no extra preview step.

Parley knows the server name, icon, ID and member count by itself, and creates an invite. If it can't create one, it asks the owner to pick a channel or paste an invite.

**Ad safety:** links are checked before publishing. Blocked domains (IP grabbers and similar) and too many links are refused; a shortened link Parley can't judge is held for staff review. This is link hygiene, not malware detection. Configure it in **Settings → Rules → Listings** and `moderation.*`.

**Paste My Own Ad** means the owner posts the real message directly in `#server-directory`:

1. Parley opens a private 3-minute posting window for that member only.
2. They send **one** advertisement message in the listings channel.
3. Parley closes the window immediately and checks the message.
4. The original user-authored message stays as the ad. Parley adds only a small action strip underneath with **Join Server** and **Request Partnership**.

This keeps the owner's formatting and custom emoji because Parley is not recreating the message. `#server-directory` is read-only for normal use; only the active poster gets a short one-message window, and Parley deletes unauthorized messages as a second safety net. Automatic Setup gives Parley the Manage Messages / Manage Permissions access it needs. Message Content Intent is enabled by Parley automatically, but it must also be switched on once in the Discord Developer Portal.

If the ad fails moderation it is removed. If staff review is required, it is held for review before publication. A user-authored ad gets one controlled ad replacement per Relist cycle. **Relist** does not make the owner paste the ad again: Parley moves a tiny one-line pointer with **View Ad / Join Server / Request Partnership** while keeping the original user-authored ad untouched. Directly editing the raw Discord message is removed so edits cannot bypass moderation; use **My Listing → Edit Ad** instead.



**Relist:** Parley's bump-style action is called **Relist**. It has a **30-minute default cooldown per server** (changeable in Settings → Rules → Listings). Owners can reach it from **My Listing** in DMs or their connected server, and the main `#server-directory` footer also has a Relist button. For a user-authored ad, Relist moves only a tiny pointer back to the newest position instead of impersonating the user or forcing them to paste the ad again. Each Relist cycle unlocks one controlled ad edit.

**Ads stay normal Discord messages:**

- Markdown, headings, spacing, custom emoji and links are kept exactly.
- `@everyone`, role and user mentions are shown but never ping anyone.

**Finding partners** shows one server at a time:

1. Choose a category.
2. See a clean server card → **Request Partnership**, **Next Server** or **View Ad**.
3. **Next Server** always shows a *different* server. Servers you already partnered with are left out of normal discovery, along with your own server, open requests, suspended or banned servers and anything whose minimum size you don't meet.
4. When the new matches run out, Parley says you are caught up and offers only the relevant next choices: **Show Past Partners** when available, **Try Another Category**, or **Show Again**.

A message on a request is optional, and after sending one you go straight back to discovering.

**Partnership requests:**

1. A request arrives as a DM with **Accept / Decline / View Ad**.
2. The Requests inbox stays calm even with several requests: choose one request from a single picker, then its actions appear.
3. When accepted, both sides get each other's contacts.

**Network ads (optional, for other servers):**

1. Press **Join Network** and pick a channel, categories and how often.
2. Pause the ads or leave the network any time.

**Commands** stay tiny:

- Everyone: `/connect` `/manage` `/find` `/network` `/help`
- Bot owner: `/setup`
- Staff, in the main server only: `/settings` and `/admin`, with `remove`, `suspend`, `ban-server`, `unban-server`, `block-user`, `unblock-user`, `listing`, `stats`, `health` and `import-settings`

---

## Troubleshooting

| You see | Do this |
|---|---|
| `setup.bat` says Python wasn't found | Install Python 3.12+ with *Add python.exe to PATH*, then run it again |
| `[Parley] DISCORD_TOKEN is not set` | Paste the token into `.env` (Windows) or Railway Variables |
| `[Parley] Discord token invalid` | Developer Portal → Bot → Reset Token, paste the new one |
| `SERVER MEMBERS INTENT is off` | Developer Portal → Bot → turn on Server Members Intent |
| `MESSAGE CONTENT INTENT is off` / direct posting unavailable | Developer Portal → Bot → turn on Message Content Intent, then restart Parley |
| `[Parley] Database connection failed` | Check `DATABASE_URL`; on Railway use `${{Postgres.DATABASE_URL}}` |
| `/setup` doesn't appear | Wait a minute, restart Discord (Ctrl+R). Re-add the bot with the link from its DM |
| "Only the owner of the bot application…" | Run `/setup` with the Discord account that owns the bot in the Developer Portal |
| A panel was deleted | `/settings → Channels → Repair Panels` (Parley also repairs panels by itself) |
| A channel was deleted | `/settings → Health Check` shows it; press **Channels** to pick a new one |
| Users see "being set up" | Parley is in TEST mode: `/settings → Live` |

Details of startup errors are written to `logs/parley.log` (Windows). Set `LOG_LEVEL=DEBUG` to also show them on screen.

---

## Advanced and recovery tools

### Put it on GitHub

```bash
git init
git add .
git commit -m "Parley"
git branch -M main
git remote add origin https://github.com/YOUR-NAME/waypoint.git
git push -u origin main
```

`.gitignore` excludes `.env`, `.venv`, databases, logs, test artifacts and settings exports. Run `git status` before the first push and check that `.env` is **not** listed. GitHub Actions (`.github/workflows/tests.yml`) runs these on every push with the locked dependencies, on both SQLite and PostgreSQL:

- compile and import checks
- a migration round trip
- the full test suite

### Environment variables

| Variable | Needed | Notes |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token |
| `DATABASE_URL` | Railway | Empty = local file `waypoint.db`. `postgres://…` URLs and `?sslmode=require` work |
| `ENVIRONMENT` | no | `production` (default in `.env.example`) or `development` |
| `LOG_LEVEL`, `LOG_FORMAT`, `LOG_TO_FILE` | no | `DEBUG` shows full error details; `json` for hosting logs |
| `RUN_MIGRATIONS_ON_STARTUP`, `SYNC_COMMANDS` | no | Both default to `true` |

Older versions configured the hub with `MAIN_GUILD_ID`, `*_CHANNEL_ID` and `STAFF_ROLE_IDS`. Those still work, and database values take priority. Go to **/settings → Channels → Save to Database**, then delete them from `.env`.

### Database and migrations

Migrations run automatically on start. By hand: `migrate.bat` (Windows) or `alembic upgrade head`.

The database itself enforces these rules:

- one listing per Guild ID
- at most one pending request per server pair
- no self-partnerships
- one ban per target

Settings live in the `runtime_settings` table, so a future web dashboard only has to write there.

### Settings CLI (emergency use)

If you are locked out of Discord settings:

```bash
python -m bot.config.cli list
python -m bot.config.cli set hub.mode live
python -m bot.config.cli set hub.staff_role_ids "[123456789012345678]"
python -m bot.config.cli unset listings.refresh_cooldown_minutes
```

A running bot picks changes up within a few minutes. The normal way to change settings is `/settings`.

### Updating dependencies

`requirements.txt` lists what Parley needs. `requirements.lock` pins the exact versions that passed the tests; the Dockerfile, `setup.bat` and CI all install the lock file. To update:

```bash
python -m venv fresh && source fresh/bin/activate   # Windows: fresh\Scripts\activate
pip install -r requirements.txt
pip freeze --exclude pip --exclude setuptools --exclude wheel > requirements.lock
pip install -r requirements-dev.txt && python -m pytest
```

Commit the new lock file only if the tests pass.

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest                                    # SQLite
TEST_DATABASE_URL=postgresql://user:pw@localhost/waypoint_test python -m pytest
```

Unit tests run against a database built by the real migrations. Discord stand-ins in `tests/fakes.py` only check Parley's own decisions.

For real Discord, use a **separate test bot**, never your production token:

```bash
set PARLEY_LIVE_TOKEN=...            # the test bot's token
set PARLEY_TEST_GUILD_ID=...         # optional
set PARLEY_TEST_CHANNEL_ID=...       # optional
python -m bot.tools.live_check         # or: python -m pytest tests/integration
```

It checks, deleting anything it creates:

- login and intents
- slash commands
- DMs
- permissions
- posting and deleting a message
- invites
- persistent buttons

### Project structure

```
bot/
  main.py        start-up (settings → logging → migrations → database → Discord)
  core.py        the bot: events, modes, staff commands, persistent buttons
  tasks.py       network rotation + maintenance
  startup_errors.py  plain-language startup errors
  commands/      /connect /manage /find /network /help /setup, staff /settings /admin
  views/         user screens (welcome, listings, partnership, management, network)
  views/admin/   setup wizard, settings pages, Test Center, moderation, health
  services/      logic without UI: listings, partnerships, network, configuration,
                 categories, setup, health, testmode, panels, permissions, moderation
  config/        settings.py (.env), runtime.py (all settings + defaults), templates.py, cli.py
  database/      models, repository, sessions, migrations runner
  tools/         live_check.py
migrations/      Alembic (0001 initial, 0002 setup/review/test mode, 0003 self-posted listings, 0004 Relist/edit-cycle, 0005 concurrent posting sessions)
tests/           unit/ (SQLite + PostgreSQL) and integration/ (live Discord, optional)
```


---

# Patch: private management cards + latest-ad links

- **My Listing now uses a compact embed** because this is a management/status screen where structure helps. It shows only server/category/members, partnership rule, Relist state and (when needed) the next ad-edit unlock.
- **View Ad now points at the real advertisement message.** For owner-posted ads it uses `listings.message_id`, never the Relist/helper card ID. After an ad replacement, the button resolves to the new message.
- **Old persistent View buttons stay useful:** their callback looks up the current listing before showing an **Open Ad** link, so an old request/message does not permanently point at a stale ad.
- **Finder View Ad stays a normal blue action and resolves the latest real ad message at click time, avoiding stale links and gray link buttons mixed into the action row.**
- **Multi-server choice screens use small embeds + one dropdown** for My Servers, List a Server, Find Partners source selection and Relist.
- **Public directory stays minimal:** advertisements remain normal messages; Parley does not add a second large embed under them.

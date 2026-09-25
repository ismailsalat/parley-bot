# Parley audit: setup & settings pass

Checklist made before this pass, and what was done about each item.

## Already working, kept as-is
- Services layer (listings, partnerships, network, moderation) and database constraints
- Bottom-panel ordering and restart-safe panels; persistent dynamic buttons
- Mention suppression; ads as normal messages; Alembic 0001; config CLI; 107 original tests

## Broken → fixed
- **Approval loophole:** editing an approved ad went live instantly. Edits now wait in
  `pending_changes` (old ad stays live) when `listings.reapprove_ad_edits` is on (default).
  `listings.reapprove_info_edits` does the same for category, minimum and invite.
- **Stale reviews:** review buttons carry a revision number. A newer edit outdates the old review,
  which can no longer approve content staff didn't see.
- **Startup tracebacks:** database, token, intent, schema and config failures print one
  `[Parley] …` line. Details go to `logs/parley.log`, or the console with `LOG_LEVEL=DEBUG`.
  No credentials are printed.
- **Unpinned dependencies:** added `requirements.lock`, used by Docker/Railway, `setup.bat` and CI.
- **Whitespace-only messages:** accepted as templates before; now rejected (found by the new tests).
- **Production-SQLite warning:** fired for normal local Windows installs; now only on Railway.

## Incomplete → implemented
- Main server, channels and staff roles are stored in the database (`hub.*` in `runtime_settings`).
  Old `.env` IDs keep working and can be saved to the database from Settings → Channels.
- `/setup` (bot owner only): Automatic Setup, Choose Channels with a permission report,
  Test Setup, and a fallback when Manage Channels is missing.
- `/settings` control center, also reachable from the DM home for staff.
- TEST / LIVE / OFF modes, Test Center, Health Check, Export/Import.
- 23 editable messages with placeholder validation; button labels/emoji; Support/Rules/Website links.
- Categories editor (rename/remove updates listings and network filters); Moderation center;
  approval queue.
- Ad preview before publishing; invite fallback (choose a channel or paste one).
- `python -m bot.tools.live_check` for a separate test bot.

## Awkward UX → improved
- Join message falls back to an obviously general channel (never a random one) and has
  Connect / Join Network / How It Works buttons.
- Home/Back on menus; menus edit themselves instead of posting new messages.
- The network page now has Pause and Leave.
- The DM home is five buttons for users; staff get one extra Settings button.

## Missing tests → added
Setup persistence, database-backed channels/staff, modes, test-mode side effects, templates,
buttons, links, settings permissions, automatic/manual setup, health, deleted channels, panel repair,
approval after edits, hot reload, reset, export/import, install URL, startup errors, the lock file,
a static button/handler audit, and rendering of every admin screen.


---

# Patch: simpler UI, calmer screens, better discovery

- **Invalid emoji (live failure):** `emoji="←"` was rejected by Discord. Added `bot/utils/emoji.py`
  (`is_valid_emoji`), replaced every built-in emoji with a curated set, refused invalid emoji on save
  and made `RuntimeConfig.button()` drop a bad stored emoji instead of rendering it. Live check now
  posts the real views so this class of bug fails there too.
- **Colour system:** blue = main action, green = approve/enable, red = destructive, grey = Back/Home
  on its own final row. Colours (and labels/emoji) are configurable per button.
- **Post My Server:** 3 steps (basics → paste or build → preview). The basics screen uses self-explanatory
  partnership/minimum wording; the creator is the default request contact and can change contacts later. Preview shows only the ad.
- **My Listing:** one connected server skips the list; the screen shows a 4-line summary with
  Relist / Edit / View Ad, Remove Listing, Home. Edit reveals its two options on demand.
- **Find Partners:** one result at a time, with a per-search seen list, fair random order, pool
  exhaustion handling and Show Again.
- **Ad moderation:** blocked domains, max links and shortened-link review/block.
- **Custom emoji:** detected and warned about before publishing.
- **Settings:** home trimmed from 15 buttons to 10 (Rules and Mode sub-menus); Appearance split into
  Buttons / Messages / Links / Theme; Test Center trimmed to 5 tests; Health Check grouped by area
  with Details and Fix Permissions.


---

# Patch: Post Ad In Channel, colour audit, partner exclusion, calmer admin

- **Foreign custom emoji solved, not just warned about:** the warning screen now offers
  **Post Ad In Channel / Continue Anyway / Edit Ad**. The owner gets a one-off, per-member posting
  window in the listings channel (granted and revoked by Parley, one message, moderated,
  auto-timeout), their message becomes the listing, Parley posts the action buttons underneath
  and the bottom panel stays below. Migration `0003` adds `listings.self_posted` and
  `listings.controls_message_id`.
- **Honest limits:** needs Manage Messages + Manage Permissions in the listings channel and the
  Message Content intent (otherwise Parley cannot read, so cannot moderate, so the button is
  hidden with the reason). Held-for-review ads are taken down while staff decide; editing an
  owner-posted ad republishes it as a Parley message.
- **Grey audit:** every `ButtonStyle.secondary` is now Back / Home / Cancel. Export Settings and
  Check Again became blue, Ban Server red, Pause Network Ads blue, How It Works blue, View Ad blue.
  A test fails the build if any non-navigation button is grey.
- **Discovery:** accepted partners are excluded from normal results and only return via
  **Show Past Partners**.
- **Admin stimulation:** settings home is five choices (Server / Appearance / Rules / Moderation /
  Tools); no admin screen offers more than six non-navigation choices, enforced by a test.


---

# Patch: clearer basics + finder empty state

- **Basics screen:** shortened to “Set up your listing.” Partnership options now read **Partnerships: Open/Closed** and minimum options read **Partner size: Any / N+ members**. First-time posting defaults partnership requests to the creator instead of making them decode a contact picker.
- **Finder empty state:** no-result searches now use a compact red **❌ No servers found** embed with a short explanation and only **Try Another Category / Show Again / Home**. Exhausted searches use the same polished style.
- **Self-post wording:** the user-facing label is now **Post Ad In Channel** instead of the vague **Post It Myself**.


---

# Patch: direct ad posting + calmer user flow

- **Paste My Own Ad is now literal:** it opens a 3-minute, one-message window in `#server-directory`; the member's original Discord message becomes the listing instead of Parley reposting it.
- **Simpler screens:** the ad-choice screen is one question with two choices, basics labels are plain language (`Any server size`, `Requests go to`), and decorative emoji were removed from this flow.
- **Self-post edits stay self-posted:** one controlled replacement is allowed per Relist cycle; Relist itself only moves Parley's directory card and never converts the owner's ad into a bot-authored message.
- **Safer posting window:** posting permission is removed immediately after the first message and the previous member overwrite is restored.
- **Message Content:** Parley requests it automatically; admins only need to enable the privileged intent once in the Discord Developer Portal.
- **Finder empty state:** kept as a small error embed with only `Try Another Category` and `Show Again`.
- **Live crash fixes included:** `show_screen` no longer sends `embed` and `embeds` together, and startup Health Check runs after background tasks start.


---

# Patch: first-time clarity and clean discovery

- **Finder result:** replaced the plain text block with a compact embed showing Category, Members and an explicitly named **Partner requirement**. Finder buttons no longer use decorative emoji.
- **Channel names:** new Automatic Setup uses `#start-here`, `#server-directory`, `#find-partners`, `#support`, `#waypoint-logs`. Legacy names are reused rather than duplicated on upgrades.
- **Server invite onboarding:** when Parley joins a server it sends one clean **Parley is connected** embed with one green **Find Partners** action, plus **Post Server Ad** and **How It Works**. Find Partners guides missing ad + Network setup automatically. The owner gets a one-time DM, and each manager who actually uses Parley gets one concise one-time DM for that server. Nothing is posted automatically just because the bot was invited.
- **No sendable channel:** Parley falls back to DMing the guild owner with the exact View Channel / Send Messages fix and `/connect` next step.


---

# Patch: Relist, locked directory, one-edit cycles

- **Relist:** added a branded one-click Relist action with a 30-minute default cooldown per server. It is available from My Listing and the main server-directory panel; the cooldown is shared by every admin of that server.
- **Direct ads stay direct:** Relisting a user-authored ad moves only Parley's clean directory card. The owner's real ad message is never impersonated or reposted by the bot.
- **One edit per cycle:** migration `0004` adds `listings.last_ad_edit_at`. A successful Relist unlocks one controlled ad replacement; another edit waits for the next Relist cycle.
- **Crash-safe posting windows:** `0004` also persists the temporary directory permission state. On restart Parley restores the member's exact previous overwrite before allowing another direct-post session.
- **Directory lock:** `#server-directory` is forced read-only for normal use, and the message listener deletes unauthorized posts even if an unusual role overwrite or Administrator bypasses the normal deny.
- **One-message window:** the active poster gets three minutes and one message. Extra rapid messages are removed; the original member overwrite is restored after the window.
- **Direct edit bypass blocked:** editing a live owner-authored ad directly causes Parley to remove that edited message and direct the owner to My Listing → Edit Ad, where moderation runs normally.
- **Cleaner directory card:** owner-authored ads get a small bot-owned card with server name, category/member count, partnership status and at most View Ad / Join Server / Request Partnership.
- **Lower stimulation:** normal workflow buttons now default to little or no decorative emoji; the main listing panel uses Post My Server / Find Partners / My Listing / Relist / Add Parley.


---

# Patch: concurrent posting windows and live-error cleanup

- Removed the channel-wide direct-post lock. Different servers can now post concurrently; only the same member or same server is blocked from opening a duplicate window.
- Self-post crash recovery now keys sessions by channel + member; migration `0005` upgrades existing databases.
- Fixed the `NameError: action_button is not defined` after a successful direct post.
- Posting-window errors now replace the flow with one clean error state instead of showing the error above the normal ad-choice screen.
- `start.bat` no longer pauses after a clean CTRL+C shutdown, reducing the confusing repeated `Terminate batch job (Y/N)?` prompts.


---

# Patch: minimalist directory + multi-server polish

- **Public directory stays about the ads:** removed the large duplicate server embed under owner-posted ads. The first post gets only a tiny one-line action strip with **Join Server / Request Partnership**.
- **Relist stays one-click without reposting the owner:** a Relist moves a compact one-line pointer with **View Ad / Join Server / Request Partnership** to the newest position.
- **Public footer trimmed:** `#server-directory` now shows only **Post My Server / Find Partners / Relist**. My Listing and Add Parley live in the places that need them instead of repeating under every ad.
- **Multiple servers:** one listed server opens My Listing immediately; multiple listings use one server dropdown instead of a grid of management buttons. DM home labels the action **My Servers** only when the user actually has multiple listed servers.
- **Cleaner onboarding:** the public join/start panels use one green **Find Partners** CTA. It routes through the shared server-ad wizard and Network channel setup only when needed; the listing action stays separate and synced everywhere, and Relist is available from management plus `/relist` for server managers.
- **Quieter Health Check:** optional Support / Staff logs channels no longer produce warning noise when intentionally unconfigured.


---

# Patch: whole-project UX review — management and ad links

A second pass reviewed the normal-user flow across listings, management, discovery, onboarding, permissions, direct posting, Relist and multi-server handling. Changes from this pass:

- **Management screen:** switched My Listing from a loose text block to a compact embed. Embeds are used where they improve scanning; raw advertisements remain normal Discord messages.
- **Latest-ad invariant:** every management View Ad path now resolves the listing's real `message_id` at click time. The bot-owned `controls_message_id` is never treated as the advertisement.
- **Stale buttons:** legacy dynamic View buttons re-resolve the current listing and offer the latest ad, so replacing an ad does not strand older UI on a dead message.
- **Finder:** View Ad remains a consistent blue action and resolves the latest real ad at click time.
- **Server pickers:** My Servers, List a Server, Find Partners source choice and Relist use one compact embed + one dropdown instead of bare text or button grids.
- **Copy:** default management wording is **View Ad**, not the ambiguous **View Listing**.
- **Directory/footer:** public feed remains minimal; the footer text now matches its Relist action.
- **Static verification:** the full Python tree compiles after the changes. Full pytest/live Discord runs still require the Discord dependency/test bot environment.

---

# Full product polish pass: latest-ad links, calmer inbox, fair discovery

- **My Listing:** now uses a compact embed where structured status is easier to scan. **View Ad** points to the listing's current real Discord advertisement message (`message_id`), never the channel and never Parley's helper/Relist card.
- **Server pickers:** My Servers, Post My Server, Find Partners and Relist use one clean select inside a small embed only when more than one server makes a choice necessary.
- **Directory restraint:** a fresh self-posted ad gets only the two useful actions underneath it; Parley's one-line server summary is reserved for a later Relist pointer so the bot does not visually compete with the advertisement.
- **Requests inbox:** multiple incoming partnership requests no longer create three buttons per request. The inbox shows one request picker; choosing one reveals Accept / Decline / View Ad for that request only.
- **Discovery fairness:** removed the old newest-200 candidate ceiling. Finder pages through the eligible database set and uses reservoir sampling, so older eligible listings do not become invisible as Parley grows.
- **Past partners:** Show Past Partners is now truly past-only instead of resetting into a mixed pool that could repeat fresh results.
- **Copy:** default management wording is **View Ad**, not the ambiguous **View Listing**.

---

# Audit: Discord's "Add your first app" checklist

**Question:** the server checklist still shows "Add your first app" although Parley is installed
and working.

**Verified against this repository (not assumed):**
- One builder, `invite_url()` in `bot/views/welcome.py`, produces every install link:
  `https://discord.com/oauth2/authorize?client_id=...&scope=bot+applications.commands&permissions=...`.
- Searching `bot/` for `oauth_url(` and `discord.com/oauth2/authorize` returns that file only, so
  there is no legacy, hand-typed or second install path. A test now enforces this.
- That URL *is* the current guild-install flow: the `bot` scope is what makes Discord install the
  app into the server. `discord.utils.oauth_url` in discord.py 2.7.1 has no `integration_type`
  parameter, so nothing was missing from the link.

**Conclusion:** nothing in Parley prevents the checklist from completing, and nothing in Parley can
complete it. That card is server UI owned by Discord and its state is not exposed to apps. The
guild onboarding API (`/guilds/{id}/onboarding`) is *member* onboarding (prompts, default channels),
not the admin getting-started checklist, so it cannot tick it. No mechanism was faked.

**Changes made, because they were genuinely missing:**
- Parley never declared an installation context. The commands now say so explicitly with
  `app_commands.allowed_installs(guilds=True, users=False)` and matching `allowed_contexts`, so the
  code agrees with the Developer Portal instead of leaving it implicit. DM access is unchanged:
  `/find`, `/manage`, `/help` keep working in a DM with Parley; `/connect`, `/network`, `/setup`
  stay `guild_only()` as before.
- `INSTALL_SCOPES` names the scopes in one place and documents why `bot` is required.
- `tests/unit/test_install.py` pins the authorize URL, both permission sets, the "no link before
  login" behaviour, that every Add Parley button uses the one builder, that only one file may build
  an install URL, and the per-command install contexts.

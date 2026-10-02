# Parley — simplified Free Post UX and staff testing fix

## Member experience
1. Open Free Listing -> Create Free Listing.
2. Choose exactly one category; the next Discord modal opens immediately.
3. In automatic mode, paste your formatted ad **once**, including at least one valid Discord invite link (regular or vanity). There is no second invite field. Confirm preview.
4. In staff-approval mode, provide an invite and a short description; after approval, post the formatted ad from My Server Listings with no deadline.
5. Existing Free Listings are managed from My Server Listings: edit, repost or delete. Selecting Create does not override the one-listing limit.

## Safety
- Ads still go to the directory as normal Markdown text, not Discord embeds. Links in ads must resolve to the advertised guild; different invites and vanity URLs for that same guild are allowed.
- '18+' or 'adults only' does not automatically mean sexually explicit. NSFW/explicit content and NSFW invite channels are blocked; a Discord `age_restricted` server is held for staff review rather than automatically declared NSFW.
- Merely submitting a public invite never grants manager permissions. Cooldown / spam safeguards remain on for ordinary members.

## Staff testing
In Settings -> Test Center -> Test Utilities:
- `Reset Cooldowns (TEST)` intentionally affects TEST records only.
- `Reset My Free Listing` is **staff-only** and requires confirmation. It removes only the acting staff account's current Free Listing (and attempts to take its live ad down), clears their `quick_post_deleted` timer and `refreshed_at`, and records an audit event. It can be used in LIVE mode, so read the confirmation carefully. It does not reset other members or connected-server listings.
- Failed Discord message removals are retained for maintenance retry by the existing panel service.

## Deployment
No schema changes or migration files were added. After backing up your database, replace the changed files, push to GitHub, and wait for SQLite and PostgreSQL Actions checks before deploying to Railway. Refresh existing welcome/rules panels if necessary.

## Verification limitations
Local `compileall`, AST checks and targeted source-level assertions passed. The complete pytest and live Discord flows were NOT run here because pinned discord.py packages could not be installed in this environment. GitHub CI and a live manual test are still required.

# Camelot posting and cooldown fix

This update supersedes the previous Camelot listing patch. No new migration or runtime dependency is required.

## What was wrong

The old Settings control wrote `listings.refresh_cooldown_minutes`, but Free Posting read `listings.quick_post_cooldown_minutes`. Runtime validation also silently forced the Free timer to at least 1440 minutes. A saved short setting could therefore leave a 20-hour wait. Existing-listing errors deliberately omitted the submitter. Reposting preserved the old ad, which made pasting replacement text confusing.

## What changed

- Free Posting and the old relist setting now share the same effective value. Saving either name updates both. A previously saved legacy value is honored when no explicit Free value exists. Connected timers remain separate.
- `/settings` → **Posting** → **Edit Repost Cooldowns** edits Free and Connected timers in minutes. Values 0–10080 are accepted; 0 disables the chosen timer. Settings changes also recalculate existing and deleted/switched listing waits.
- **Reset My Repost Timer** is a confirmed, staff-only recovery action. It clears the acting account's Free timer while preserving its ad, submitter, messages, and review/moderation state. Other members' timers are unchanged.
- My Free Listing, duplicate-listing errors, and staff details identify the recorded submitter. Old records use their submission audit history when available; missing history is explicitly labeled unknown. Newly published Free ads display attribution when it fits within Discord's message limit. Mentions do not ping users.
- **Check Cooldown** remains available on an existing listing. **Edit Ad** replaces text without relisting. **Repost** republishes the saved ad; it is not a text edit. Successful submissions return to listing controls.
- **Retry Saved Ad** delivers an unpublished saved ad without claiming a new repost cycle. Concurrent recovery attempts in the same bot process do not duplicate it.
- Failed repost deliveries restore the previous timer reservation without overwriting a newer successful publication. Auxiliary panel/staff-control failures no longer turn a successful ad delivery into a reported posting failure.
- The exact Camelot ad, including Unicode, Markdown, spacing, and the repeated same-server invite, passes regression tests. Same-server vanity invite edits keep the saved server identity.
- Server identity and management permissions remain checked. The recorded submitter of a Free ad is not automatically a verified server owner.

## Install

For the patch archive, extract its contents into the project folder containing `bot`, `tests`, and `requirements.lock`, accepting file replacements. The full archive contains the complete updated project in `parley-bot-main`.

Commit and push the update. Confirm both GitHub database jobs and the deployment status. After Railway restarts, `/settings` → **Posting** should show **Edit Repost Cooldowns** and **Reset My Repost Timer**. If those buttons are absent, the deployed process is still using older code.

## Unblock your current test

1. Open `/settings` → **Posting** → **Reset My Repost Timer** and confirm. This keeps your ad and clears only your own Free timer.
2. Use **Edit Repost Cooldowns** to choose the ongoing Free and Connected waits. For example, enter 30 for a 30-minute Free wait, or 0 to disable it. The default remains 1440 minutes for Free and 30 for Connected until configured; this update does not silently disable all members' cooldowns.
3. Open **My Server Listings**. To replace Camelot's text, choose **Edit Ad** and paste the Camelot ad. To move the existing saved ad back up, choose **Repost Existing Ad**. A saved unpublished ad shows **Retry Saved Ad**.
4. If the listing was submitted by another account, the message identifies that account. Use the original account, verified **Connected Servers** management, or staff review. A public invite alone cannot transfer control of another listing.

## Verification

568 passed, 1 skipped on Python 3.12 with the project's exact locked dependencies. The skip is the live Discord integration test because no test bot token is configured. Four existing discord.py UI-label deprecation warnings remain; there are no test failures.

Compilation, pyflakes, startup imports, persistent component registration, SQLite Alembic upgrade/check/downgrade/upgrade/downgrade all passed. PostgreSQL could not start in this workspace. The existing GitHub workflow still runs the full suite and migration checks on SQLite and PostgreSQL 16; that hosted run was not triggered or observed here. This archive has not been deployed to Railway or tested against the live Camelot listing.

The local tests simulate Discord messages for posting, retries, and UI behavior; they do not prove production permissions or network delivery. Detailed test output is in `validation/`.

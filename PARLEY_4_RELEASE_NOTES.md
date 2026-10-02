# Parley 4 — Simple free ads, approvals, rules, and recovery

## Changes

* **Automatic mode (default):** Member selects one category, enters server invite, pastes the complete Discord Markdown ad into the text form, reviews it and confirms. Parley validates the invite and posts the text as a *normal Discord message* in Server Directory. Unsafe external links may still require staff review.
* **Staff approval mode:** Member submits a Discord invite, one category, and a short summary. Parley routes the application to the new private `#ad-approvals` channel instead of `#parley-logs`. Staff may approve/reject or ban. On approval the member is notified in Server Directory and can use **My Server Listings > Post Approved Ad** at any future time. No temporary channel or deadline. The final submitted formatted text is then validated and published; no placeholder summary ad is published.
* **Rule channel:** `#server-rules`, read-only, explaining no NSFW/adult servers, no ToS violations, scam links, impersonation, unsolicited mass pings or attempts to bypass cooldowns.
* **Upgrades:** On restart, if the existing hub is configured and the bot has channel-management permissions, create/reuse the two new channels and save their IDs. Approval channels are kept private and rules channels read-only. If permission repair fails, Parley will not route reviews to an unverified channel.
* **Perks refresh:** Replace prior persisted panels, clean up old bot-authored Perks posts on startup, and retry if Discord won't delete an old panel instead of stacking duplicates.
* **Outages:** Listings and approval status stay in PostgreSQL. Pending automatic publication and missing approval notifications are retried. Abandoned *legacy* temporary ad rooms are still cleaned up on startup; no new temp ad rooms are created in this free-listing flow.
* **Security:** The bot compares each invite's Discord guild ID (including vanity invites), never assigns owner rights to unverified posters, disables mass mentions, and publishes as plain content, not an embed.

## Before deployment

1. **Back up PostgreSQL** in Railway (particularly important if you haven't yet backed it up).
2. Push these files and wait for both SQLite and PostgreSQL GitHub Actions checks to pass.
3. Deploy the new code; migration `0011` adds `listings.awaiting_ad` and `listings.approval_notice_message_id`. Your previous database was at `0010`; never run a migration on the Waypoint service. Check the Parley container using `python -m alembic current` and `python -m alembic heads`; Parley's startup migration feature may automatically update it.
4. Confirm Parley has **Manage Channels**, **Send Messages**, **Read Message History**, and appropriate permission-overwrite management privileges in the main server; staff should be configured using `/setup` or Settings.
5. Use Parley Settings to switch Approval Required ON/OFF; Automatic means OFF, Staff Approval means ON.
6. For an existing hub, check `#ad-approvals` is visible only to authorized staff and `#server-rules` is read-only. Refresh the existing panels. Confirm `#server-directory` is correctly configured and writable by Parley.

## Manual acceptance tests in Discord

* **Automatic:** Submit a Free listing with a Markdown ad and two distinct valid invites to the same guild; confirm the directory message appears in plain text, no embed, and one active listing is shown in My Server Listings.
* **Automatic recovery:** Temporarily make the directory unwritable; submit an ad, restore permissions and wait for maintenance. It should publish from saved state.
* **Staff approval:** Turn approval ON, submit only invite/category/summary and check the review appears in private #ad-approvals. Approve and confirm the summary is NOT posted as a public ad; verify the submitter receives a directory notice and can post the final formatted ad hours later without a time window.
* **Permissions:** Other users may not claim, edit, relist or delete a Free submission; staff review actions must check current permissions. Unrelated Discord invites must be rejected.
* **Restart:** Restart with pending staff application, approved-not-yet-posted application, and an unconfirmed legacy temp channel. Ensure state persists, notices retry, and the legacy temp channel is cleaned up when possible.
* **Perks:** Refresh panel twice, restart, verify old bot-owned Parley Perks entries are deleted rather than accumulating.

## Validation status

Syntax compilation and independent AST import-use checks passed in the editing container. Alembic PostgreSQL *offline SQL generation* ran through revision 0011. A full pytest run and SQLite migration check could not complete here because the local container lacks `discord.py` / `aiosqlite` dependencies and cannot fetch them; GitHub Actions must confirm the full suite. This release has not been deployed or tested against live Discord by the assistant.

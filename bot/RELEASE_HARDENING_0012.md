# Parley 4 — audit follow-up (migration 0012)

This release is based on the user-provided `Parley_4_Camelot_Listing_Fixed_Full (1).zip`.

## What changed

1. **Discord-hosted image attachments:** `cdn.discordapp.com/attachments/...` and `media.discordapp.net/attachments/...` image URLs are recognized without classifying them as unrelated external websites. Hostname spoofing, unknown file types and other external URLs remain subject to the existing review policy. **Origin validation does not scan images for NSFW or harmful content.**
2. **Update Invite:** The original Free Listing submitter can click **Update Invite** in My Free Listing, paste a replacement regular/vanity invite, and have Discord verify that it belongs to the *same guild ID*. It updates the stored **Join Server** button and recognized old invite in ad text, edits the live listing in place where available, and does **not** reset relist or ad-edit cooldowns. If the post cannot be edited immediately, the hourly listing refresh can retry. Connected guilds and other submitters cannot use this path.
3. **Durable deletion queue:** A relist records old message IDs in `pending_message_deletions` in the same database transaction as the new message ID before trying to delete the old copy. Failures are retried in bounded batches by maintenance, including after restart. Older, repeatedly failing entries cannot indefinitely starve newer ones because retries increment an attempt count. Owner-authored directory cards and expired listing messages also use the cleanup queue. A missing directory message is checked using `fetch_message` rather than assuming an existing partial message proves publication.
4. **Historical panel cleanup:** Routine cleanup stays limited to recent messages, but a staff-triggered **Fix & Test → Repair Panels** scans all available channel history (Discord permissions/rate limits permitting) and deletes only recognizable old Parley-owned entry panels/rules, preserving the currently recorded panel and user advertisements.

## Database and rollout

- **New Alembic migration: `0012`**, `0011 -> 0012`.
- **Back up Railway PostgreSQL before deploying.** In the user's existing setup, Parley can run migrations automatically at startup if `RUN_MIGRATIONS_ON_STARTUP=true`; check startup logs and `python -m alembic current` after deploying. Do not blindly run migrations twice or reset the database.
- Do not deploy merely because compilation passes. Run `verify_release.cmd` on Windows or:

```shell
python -m compileall -q bot migrations
python -m pyflakes bot tests migrations
python -m pytest -q
```

- Confirm both SQLite and PostgreSQL GitHub Actions jobs pass on the *latest commit*. Then test in a private Discord server: update invite; relist with deliberately missing Delete Message permission and subsequently restore permission; restart/retry; and staff Repair Panels in a channel with older Parley messages.

## Verification scope in this environment

- Python compilation and AST import-use checks completed.
- Isolated live-SQLite ORM checks passed for media classification, same-guild invite replacement, prevention of a different-guild update, unchanged refresh timestamp, queue deduplication, and queue clearing.
- Migration 0012 SQL generated and its resulting SQLite table matched ORM metadata without schema differences.
- **Full pytest, PostgreSQL runtime, and live Discord interactions were not executed here:** `discord.py` and `aiosqlite` were not available, and dependency downloads failed due to network resolution. The included regression tests still require GitHub Actions / the user's machine to run. A green CI result is not guaranteed or claimed.

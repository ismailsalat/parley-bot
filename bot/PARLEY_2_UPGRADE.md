# Parley — Quick Post / Connected Network deployment notes

## What this release does

- New Quick Post: no OAuth and no installation. Paste a Discord invite, pick a category, add a short description, review a preview, and submit.
- Quick Posts are ALWAYS sent for staff review, even when global listing approval is disabled. Configure a staff log/review channel before launch.
- Quick Post is **not ownership verification**. Submitters cannot control partner posts, server permissions, requests, or other accounts' server listings.
- One unconnected listing per Discord user. A unique, nullable DB column enforces the limit under concurrent submissions.
- Quick Post republishes an approved existing listing after a 24-hour cooldown; connected listings use the configured 30-minute relist cooldown.
- A guild is claimed only after Parley is installed and the user proves live Manage Server/Administrator permission. The original submitter loses Quick Post control for that guild.
- The existing partnership and approved network posting functionality stays intact.
- Existing Discord OAuth verification infrastructure remains for compatibility, but it is no longer presented from the normal hub Post flow.

## Deploy

1. Back up Railway PostgreSQL.
2. Deploy code and run `alembic upgrade head` before starting the bot. The new revision is `0009`.
3. Make sure the main hub has an active Server Directory and **staff log/review channel** (`hub.log_channel_id`). Without one Quick Post deliberately refuses new submissions rather than publishing unreviewed ads.
4. Keep existing bot and OAuth credentials secret; none are included in the ZIP. No OAuth server is required for Quick Post.
5. Refresh/repost the welcome and directory panels via Parley's existing setup tools to show the updated copy; previously posted panel text does not change by itself.
6. Test from a non-admin user with a valid public invite, and from a manager after installing Parley in a separate test guild.
7. If you previously saved overrides under `runtime_settings` for panels and listing cooldowns, those **override code defaults**. Update them deliberately through the existing `/settings` UI or `python -m bot.config.cli`.

## Safety / limitations

- A public invite identifies a server, **not** who owns/manages it. Quick ads are explicitly labelled unverified, and manual staff approval is required.
- Quick Post is a directory submission, not permission to send unsolicited messages into someone else's server.
- A Quick Poster can only repost their own unclaimed listing; they cannot manage a connected guild. Connecting does not automatically approve partnerships.
- Installing the bot DOES grant the explicitly displayed Discord bot permissions; the server owner must opt in, and can remove Parley or disable Network settings at any time. Do not claim installing a bot requires no permissions.
- For trust, welcome-panel broadcast mentions are disabled by default. Saved runtime settings may still override this: check `/settings`.
- Free listings are not endorsements or verification of the advertiser's server ownership. Owners can contact Parley moderators about abuse or mistaken listings.
- Discord invite metadata and approximate counts are best-effort; unavailable data is shown as unknown rather than invented.
- A production Discord/Railway end-to-end test is required before launch. This ZIP is not a live deployment.
- A 30-minute eligibility window is **not** a promise of ranking/traffic; use fair directory rotation and inspect real usage as the network grows.

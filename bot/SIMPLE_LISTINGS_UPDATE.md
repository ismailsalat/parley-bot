# Parley — My Server Listings simplification

This update targets the **My Server Listings** menu and does **not** change the database schema; migration 0010 remains the latest.

## What members see

- **Free / Unverified** — the member submitted a public server invite, without proving control of that Discord server. They can edit their own ad (when eligible), repost it after cooldown, or delete it with confirmation. They cannot manage the server or make partnership requests on its behalf.
- **Verified manager** — Parley confirms that the member manages the connected guild, or it has a previously verified manager delegation. Connected managers continue to use the existing listing controls.
- When a member has both, one selector distinguishes them. When they have only a Free Listing, its dashboard opens directly.
- A pending or suspended Free Listing disables invalid Edit/Repost actions, but still offers Delete. A staff-rejected ad offers **Submit Corrected Ad** after cooldown.

## Deletion and safety

- Delete asks for confirmation, then marks the listing `removed` in PostgreSQL. Pending staff review becomes invalid and stale approval buttons cannot restore it.
- Deleting clears the member's one-Free-Listing allocation; a persisted cooldown still stops instant delete/recreate spam.
- Deleted listings retain a tombstone and an audit record: other users cannot claim the old guild listing by merely pasting its public invite. The original submitter can resubmit after cooldown unless staff took later action. Verified managers can claim control using the normal permissions flow.
- The bot attempts to remove the public directory and partner-board posts. If Discord is unreachable, the message IDs **remain in the database** and periodic maintenance retries cleanup after reconnection/restart.

## Deployment

Replace the enclosed files in the root of the existing Parley repository, preserving the directory structure. Run GitHub Actions SQLite and PostgreSQL CI before deploying. No new migration is introduced by this change; do not reapply older migrations just for this patch.

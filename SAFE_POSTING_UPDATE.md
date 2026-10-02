# Parley — Safe Posting / Crash Recovery Update (migration 0010)

## New listing experience

Users click **Post a Server Free** in the main Parley Discord hub. They select **one** configured category (default: Gaming, Anime, Social, Community, Roleplay, Creator) and choose:

- **Write an Ad** — supply the invite plus a short description (550-character description limit); confirm a preview. The bot formats the listing.
- **Paste Existing Ad** — supply an invite, then go to a private, newly created `parley-temp-ad-<user_id>` room. Paste one TEXT-ONLY advertisement and review a complete preview with canonical invite attached. Press Confirm Ad, Try Again, or Cancel. Messages with attachments/stickers are not supported in this workflow. No OAuth is required.

Quick Post always identifies the target Discord guild by fetching its invite. It **does not verify ownership** and never gives the poster management or partnership permissions. One unconnected listing per Discord user and one listing per guild. Quick posters repost after 24 hours. Existing approval, ban and guild-specific cooldown checks are retained.

Connected management continues to use permissions in the actual connected guild, and the connected relist cooldown remains separately configurable (30 minutes by default).

## Automatic vs manual approval toggle

Go to `/settings` → **Listings** → **Approval required**.

- OFF (default): ads publish automatically AFTER validation and explicit confirmation. Ads with external/unfamiliar links are held for staff review.
- ON: new submissions and eligible edits require a staff action before publishing.

Do **not** disable content safety checks, regardless of the toggle. Staff-review delivery requires a working private staff log/review channel. If staff notification temporarily fails after the draft is saved, maintenance retries without automatically approving it. The old public ad can remain visible while an edit waits for review.

Edits do not bump a listing or reset its repost cooldown; a Quick Listing may be changed once within the current repost cycle. A guild ban or suspension prevents reposting by changing invites/accounts.

## Temporary draft room recovery

- The session's channel ID, target server ID, submitter ID and expiry time are stored in PostgreSQL (`temporary_ad_sessions`, migration `0010`). **The unconfirmed ad message is not stored in the listing database.**
- A single private room per user; maximum 15 active rooms; default expiry at 10 minutes. The room denies viewing to `@everyone`; only the author, Parley, and configured staff roles can access it.
- The bot saves a confirmed advertisement **to the durable listing database before** deleting the room. If posting to Discord fails after confirmation, the saved listing remains and maintenance can retry publication (or notify staff if it requires review).
- After normal submit, cancellation, or timeout, the room is deleted, then its recovery row removed. If deletion fails, the row remains to retry.
- On a **fresh bot restart**, Parley deletes ALL leftover tracked private draft rooms, even if they have time remaining, because their message listeners/buttons belonged to the previous process. It also sweeps orphan draft rooms carrying the reserved `parley-temp-ad-` prefix. Failures are logged and retried.
- During uninterrupted uptime, maintenance deletes expired draft rooms; it never deletes currently valid rooms merely for being old. If the bot loses connection briefly, expired records are cleaned at the next maintenance tick or startup.
- If the Discord API denies deletion (e.g., missing Manage Channels), staff must correct the permission and remove orphan channels manually if necessary. Recovery cannot guarantee deletion when Discord is unavailable.

## Staff moderation

`/settings` → Moderation → Search Listing (or `/admin`). Staff can remove an advertisement, suspend for 1 hour, 12 hours, 1, 3, 7, or 30 days, suspend indefinitely, restore, ban or unban a guild, or block a submitter. Timed suspensions are restored by scheduled maintenance without silently approving pending ads. Commands revalidate CURRENT staff permissions on use.

Discord cannot make buttons under public messages invisible to non-staff. Therefore the bot uses **separate private staff controls** in the staff channel, tied to each published guild. Staff actions take down that guild's directory and partner-board posts through the shared listing service. Staff can always use `/settings` or `/admin` as a fallback.

## Deployment checklist

1. Back up Railway PostgreSQL before deploying.
2. Copy the updated files into your existing Parley checkout; don't delete `.git`, `.env`, local secrets or stored app data.
3. **Run `alembic upgrade head`** against your Railway database before starting the new bot; the new code expects `0010` schema.
4. Ensure main server ID, directory channel, staff log/review channel, and staff roles are configured.
5. In Discord Developer Portal, enable the **Message Content** privileged intent for the bot. The code also checks and refuses private paste if this intent is disabled.
6. Give the bot **Manage Channels**, View/Send Messages and Read Message History in the main guild, so it can create/clean temporary rooms.
7. Open `/settings` → Listings: turn **Approval required** OFF for automatic publishing, or ON for staff review.
8. Exercise BOTH creation methods, abandon a paste channel, restart the bot, then confirm the abandoned room was deleted. Confirm a saved ad is still listed, and a banned guild cannot repost.
9. Confirm `test (sqlite)` and `test (postgres)` are green in GitHub Actions before trusting the deployment.

## Limits / caveats

A text ad may contain Discord custom emoji syntax that doesn't display if Parley lacks access to the originating emoji; preview the rendered result. Paste mode currently rejects attachments, stickers and files. No system can prevent all phishing or identity misrepresentation by an unverified submitter; staff bans, blocked words, domains and user reporting are still necessary. No auto-posting to other communities occurs without the existing opt-in/approval partnership system.

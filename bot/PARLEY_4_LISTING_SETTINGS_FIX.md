# Parley 4: simpler Free Listing management and safer rules recovery

## For server members

- My Free Listing now shows a compact status and an accurate cooldown countdown.
- **Switch Server**: requires confirmation, removes the old Parley listing, and frees the one-listing slot. The original posting cooldown still applies to a new server.
- **Delete Listing**: requires confirmation even for a staff-removed Free Listing. Both old public boards are cleaned through the existing retryable takedown mechanism.
- **Submit Corrected Ad** remains available for staff-removed listings once cooldown permits. Switching away from a staff-removed/suspended listing does not permit the removed guild to be reclaimed by bypassing moderation.
- **Check Cooldown** refreshes the countdown if a deleted listing is waiting.
- The original server is never deleted; only its Parley listing and posts are affected.

## For staff

- `/settings` is now a small landing page: Posting, Review & Safety, Channels, Fix & Test, Advanced.
- Posting lets staff toggle manual approval versus automatic publishing. Rules still apply in either mode.
- Fix & Test contains **Repair Panels**, **Sync Commands**, **Health Check**, and a confirmation-gated **Reset My Free Test** for the acting staff account only. No member cooldowns are reset in bulk.
- Older configuration pages remain under Advanced rather than being removed.
- Settings exports are now named `parley-settings.json`.

## Rules channel duplicates

- Static panel refresh and restart/repair paths share a lock, avoiding competing operations within a bot process.
- Startup/periodic orphan cleanup now recognizes old text-only rules messages, not just buttons and perks panels. Only bot-authored messages bearing the Parley Server Rules heading are candidates; the database-tracked current panel stays.
- If the bot cannot delete a stale message due to missing permissions, it logs the failure; repairs can be retried.
- For multi-replica deployments, use one active Parley bot instance; process-local locks cannot coordinate replicas.

## Verification and rollout

- Run `python -m compileall -q bot migrations tests` and `python -m pytest -q` with dependencies from `requirements-dev.txt`.
- Run both GitHub SQLite and PostgreSQL jobs before deploying. This environment did not have discord.py, so local pytest collection could not complete.
- No new database migration is required; migration `0011` remains current for Parley 4.
- Back up Railway PostgreSQL before deploying. When checks pass, deploy and test Free Listing Switch/Delete, removed-listing release, cooldown after switch, rules panel restart cleanup, Settings buttons and slash-command sync in Discord.

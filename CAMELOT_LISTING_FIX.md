# Parley existing-listing ownership and navigation fix

## What changed

- If the user already owns an active Free Listing, the post form returns them to their own listing controls instead of giving a dead-end error.
- If the user has a Free Listing for a different guild, it explains how to use Switch Server. This does not waive their 24-hour cooldown.
- If an advertised guild already belongs to a different Free Listing submitter, a connected/legacy listing, or a protected removed record, the bot shows a readable explanation and safe next-step buttons: My Server Listings, Connected Servers, and Contact Staff where a support URL is configured.
- Existing guild identity is based on Discord's guild ID, never an invite code or vanity slug.
- A self-deleted Free Listing can still be restored only by the original submitter after the cooldown, unless a verified manager or a later staff action blocks restoration.
- An invite does not create management authority. This patch deliberately does not transfer a listing between accounts without independent verification.
- If a guild became Connected but stale Free submitter metadata remains, Free actions cannot edit, repost, delete, or publish it; the user is redirected to verified Connected management.

## Changed code

- bot/services/quick_post.py
- bot/views/quick_post.py
- tests/unit/test_quick_post.py (regression coverage)

## Testing and deploy

No new migration is required. Existing migrations are unchanged.

Run in a Python 3.12 environment with the project's requirements-dev.txt installed:

    python -m compileall -q bot migrations
    python -m pyflakes bot tests migrations
    alembic upgrade head
    alembic check
    python -m pytest -q

GitHub CI also tests both SQLite and PostgreSQL. Verify both workflows before deploying. Local full pytest was not available in the authoring environment because the locked discord.py/aiosqlite dependencies were inaccessible. The code passed compilation and static checks, plus focused SQLite ownership tests exercising the real SQLAlchemy tables and query logic.

## Live manual checks

1. With the account that created Camelot's Free Listing, select Free Post: expect My Free Listing controls, not duplicate creation.
2. With a different account, try another invite or vanity for the same Camelot guild: expect an ownership conflict and management/help navigation, with no change to existing ad or ownership.
3. After the original account deletes its listing and the cooldown expires, the same account may submit again, but a different account may not reclaim the tombstone.
4. If Camelot is already a legacy/connected listing, manage through verified connected-guild permissions or ask staff to review the account/ownership mismatch.
5. Confirm edits, deletions, relisting, and cooldowns still behave as before.

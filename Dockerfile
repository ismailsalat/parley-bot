# Waypoint - production image (used by Railway, works with any Docker host)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Exact, verified versions (see requirements.lock)
COPY requirements.lock .
RUN pip install -r requirements.lock

COPY bot ./bot
COPY migrations ./migrations
COPY alembic.ini LICENSE ./

# Run as an unprivileged user
RUN useradd --create-home --uid 10001 waypoint && chown -R waypoint:waypoint /app
USER waypoint

# Configuration comes from environment variables (Railway "Variables").
# Only DISCORD_TOKEN and DATABASE_URL are needed; everything else is set in Discord.
# Migrations run automatically on startup (RUN_MIGRATIONS_ON_STARTUP=true).
CMD ["python", "-m", "bot.main"]

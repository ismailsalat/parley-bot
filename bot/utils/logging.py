"""Logging setup with secret redaction."""

from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path

from bot.config.settings import PROJECT_ROOT, Settings

TEXT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


class RedactSecretsFilter(logging.Filter):
    """Replaces known secret values (token, database password) in every record."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _DropVoiceWarnings(logging.Filter):
    """Parley never uses voice, so discord.py's 'voice will NOT be supported' notice is noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "voice will NOT be supported" not in record.getMessage()


DETAILS_LOGGER = "waypoint.details"


def details_logger() -> logging.Logger:
    """Tracebacks for startup failures: written to logs/parley.log (and the console only with LOG_LEVEL=DEBUG)."""
    return logging.getLogger(DETAILS_LOGGER)


def configure_logging(settings: Settings) -> None:
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter: logging.Formatter = JsonFormatter() if settings.log_format == "json" else logging.Formatter(TEXT_FORMAT)
    redactor = RedactSecretsFilter(settings.secrets())

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(redactor)
    root.addHandler(console)

    if settings.log_to_file:
        log_dir: Path = PROJECT_ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "parley.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(TEXT_FORMAT))
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)
    else:
        file_handler = None

    details = details_logger()
    details.propagate = False
    details.setLevel(logging.DEBUG)
    for handler in list(details.handlers):
        details.removeHandler(handler)
    if file_handler is not None:
        details.addHandler(file_handler)
    if settings.log_level == "DEBUG":
        details.addHandler(console)
    if not details.handlers:
        details.addHandler(logging.NullHandler())

    # discord.py is chatty at INFO (gateway details); keep it at WARNING unless debugging.
    library_level = logging.DEBUG if settings.log_level == "DEBUG" else logging.WARNING
    for name in ("discord", "discord.http", "discord.gateway"):
        logging.getLogger(name).setLevel(library_level)
    client_logger = logging.getLogger("discord.client")
    client_logger.setLevel(logging.INFO if library_level != logging.DEBUG else logging.DEBUG)
    client_logger.addFilter(_DropVoiceWarnings())
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("alembic").setLevel(logging.INFO)

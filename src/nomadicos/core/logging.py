"""Centralized secret redaction (BP §256) + structured logging (BP §134).

Never log raw secrets. Redaction is centralized so every subsystem inherits it.
"""

import json
import logging
import re
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

REDACTED_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "private_key",
        "authorization",
        "cookie",
        "session_key",
        "credential",
        "credentials",
    }
)

_SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9]{8,}|Bearer\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
_MASK = "***REDACTED***"


def redact_value(value: Any) -> Any:
    """Redact secret-looking strings (BP §256)."""
    if isinstance(value, str):
        return _SECRET_PATTERN.sub(_MASK, value)
    return value


def redact(data: Any) -> Any:
    """Recursively mask values under sensitive keys and redact secret strings."""
    if isinstance(data, dict):
        return {
            key: _MASK if str(key).lower() in REDACTED_KEYS else redact(value)
            for key, value in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [redact(item) for item in data]
    return redact_value(data)


class StructuredFormatter(logging.Formatter):
    """Emit JSON log lines; extra fields under ``record.fields`` are redacted (BP §134)."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", None)
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if fields:
            payload["fields"] = redact(fields)
        if record.exc_info:
            exc_type = record.exc_info[0]
            payload["exception"] = (
                f"{exc_type.__name__}: {record.exc_info[1]}" if exc_type else "unknown"
            )
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", log_file: str | Path | None = None) -> None:
    """Install the structured handler on the root logger exactly once.

    With ``log_file`` the structured JSON lines go to that file instead of the
    console (interactive TUIs stay clean); without it they go to stderr."""
    root = logging.getLogger()
    if getattr(root, "_nomadicos_configured", False):
        root.setLevel(level.upper())
        return
    handler: logging.Handler
    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    root.handlers = [handler]
    root.setLevel(level.upper())
    root._nomadicos_configured = True  # type: ignore[attr-defined]


class RedactingLoggerAdapter(logging.LoggerAdapter):
    """Redacts ``fields`` at emission time (defense in depth with the formatter)."""

    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> tuple[str, MutableMapping[str, Any]]:
        extra = kwargs.get("extra")
        if isinstance(extra, dict) and "fields" in extra:
            extra["fields"] = redact(extra["fields"])
        return redact_value(msg), kwargs


def get_logger(name: str) -> logging.LoggerAdapter:
    """Return a redacting logger; pass ``extra={"fields": {...}}`` for structured data."""
    return RedactingLoggerAdapter(logging.getLogger(f"nomadicos.{name}"), {})

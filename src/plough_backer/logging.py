"""Structured JSON logging with secret redaction (README §54, §55).

Usage: `log.info("trade_executed", extra={"signal_id": "PB-000184", "executed_lot": lot})`.
The message is the event name; `extra` keys become top-level JSON fields.
"""

import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

REDACTED = "***"
_SENSITIVE_KEY = re.compile(r"password|passwd|token|secret|api_hash|authorization", re.IGNORECASE)
_RESERVED = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")  # never float: "0.022500" stays exact
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: REDACTED if _SENSITIVE_KEY.search(str(k)) else _scrub(v) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_scrub(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Known secret values are masked in the final text."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        # Longest first so a secret containing another secret is fully masked.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(_scrub({k: v for k, v in vars(record).items() if k not in _RESERVED}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        text = json.dumps(payload, default=_json_default, ensure_ascii=False)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
            # Also catch the JSON-escaped form (secrets containing quotes/backslashes).
            text = text.replace(json.dumps(secret)[1:-1], REDACTED)
        return text


def configure_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    """Replace root handlers with a single JSON stderr handler. Idempotent."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

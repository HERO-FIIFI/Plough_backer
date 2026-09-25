import json
import logging
import sys
from decimal import Decimal

from plough_backer.logging import REDACTED, JsonFormatter

SECRET = 'p@ss"w0rd'  # contains a quote: must be caught in JSON-escaped form too


def _format(msg: str, secrets: list[str] | None = None, **extra: object) -> dict[str, object]:
    record = logging.makeLogRecord({"msg": msg, "levelname": "INFO", "name": "t", **extra})
    line = JsonFormatter(secrets or []).format(record)
    assert "\n" not in line
    return json.loads(line)  # type: ignore[no-any-return]


def test_structured_fields_are_top_level_and_decimals_exact() -> None:
    out = _format("trade_executed", signal_id="PB-000184", theoretical_lot=Decimal("0.022500"))
    assert out["event"] == "trade_executed"
    assert out["signal_id"] == "PB-000184"
    assert out["theoretical_lot"] == "0.022500"
    assert out["level"] == "INFO"


def test_known_secret_values_are_redacted_everywhere() -> None:
    out = _format(f"login failed with {SECRET}", [SECRET], detail={"note": SECRET})
    text = json.dumps(out)
    assert "w0rd" not in text
    assert REDACTED in out["event"]  # type: ignore[operator]


def test_sensitive_keys_are_redacted_even_if_value_unknown() -> None:
    out = _format("request", request={"login": 1, "password": "x1", "bot_token": "y2"})
    assert out["request"] == {"login": 1, "password": REDACTED, "bot_token": REDACTED}


def test_exception_text_is_redacted() -> None:
    try:
        raise RuntimeError(f"auth {SECRET}")
    except RuntimeError:
        record = logging.makeLogRecord({"msg": "boom", "exc_info": sys.exc_info()})
    line = JsonFormatter([SECRET]).format(record)
    assert "w0rd" not in line
    assert "RuntimeError" in line

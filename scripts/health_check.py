"""Health check (README §56, §74): `python scripts/health_check.py`.

Checks what exists today; components from later phases report NOT_IMPLEMENTED rather
than a fake green. Exit 0 only if every implemented check passes.
"""

import sys

from plough_backer.config import load_settings, load_sources, load_symbols
from plough_backer.exceptions import PloughBackerError
from plough_backer.persistence.database import assert_migrations_current, make_engine


def main() -> int:
    results: dict[str, str] = {}
    try:
        settings = load_settings()
        load_symbols()
        load_sources()
        results["Config"] = "OK"
    except PloughBackerError as exc:
        print(f"Config:     FAIL  {exc}")
        return 1

    engine = make_engine(settings.database_url)
    try:
        with engine.connect() as conn:  # BEGIN IMMEDIATE takes the write lock; nothing written
            conn.begin()
            conn.rollback()
        results["Database"] = "OK (writable)"
    except Exception as exc:
        results["Database"] = f"FAIL  {exc}"
    try:
        assert_migrations_current(engine)
        results["Migrations"] = "OK"
    except Exception as exc:
        results["Migrations"] = f"FAIL  {exc}"
    engine.dispose()

    for component in ("Telegram", "MT5", "Scheduler"):
        results[component] = "NOT_IMPLEMENTED"
    results["Mode"] = settings.execution_mode

    for name, status in results.items():
        print(f"{name + ':':<11} {status}")
    return 0 if not any(str(s).startswith("FAIL") for s in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

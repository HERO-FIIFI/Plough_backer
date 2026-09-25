"""Entry point: `python -m plough_backer.main` (README §74).

§30 restart sequence: 1-2 SQLite + migration gate and 5 config (bootstrap), then
runtime.run(): 3 Telegram, 4 MT5, 6-10 recovery + reconciliation, 11 resume monitoring.
Exits non-zero before touching Telegram/MT5 if any open trading decision is unset.
"""

import asyncio
import logging
import sys

from sqlalchemy import Engine

from plough_backer import SPEC_VERSION, runtime
from plough_backer.config import Settings, load_settings, load_sources, load_symbols
from plough_backer.exceptions import ConfigurationError, MigrationStateError
from plough_backer.logging import configure_logging
from plough_backer.persistence.database import (
    assert_migrations_current,
    make_engine,
    session_factory,
)

log = logging.getLogger(__name__)

EXIT_CONFIG_ERROR = 2


def bootstrap(settings: Settings) -> Engine:
    """Validate config + database. Raises ConfigurationError / MigrationStateError."""
    configure_logging(settings.log_level, settings.secret_values())
    symbols = load_symbols()
    sources = load_sources()
    engine = make_engine(settings.database_url)
    assert_migrations_current(engine)
    log.info(
        "bootstrap_complete",
        extra={
            "spec_version": SPEC_VERSION,
            "execution_mode": settings.execution_mode,
            "risk_mode": int(settings.default_risk_mode),
            "progression_scope": settings.progression_scope,
            "equity_lock_enabled": settings.equity_lock_enabled,
            "symbols": len(symbols.symbols),
            "sources": len(sources.sources),
        },
    )
    return engine


def main() -> int:
    try:
        settings = load_settings()
        engine = bootstrap(settings)
    except (ConfigurationError, MigrationStateError) as exc:
        print(f"startup refused: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    missing = settings.runtime_missing()
    if missing:  # README §76: open trading decisions are never defaulted
        engine.dispose()
        print(f"startup refused: set {', '.join(missing)} in .env", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    try:
        asyncio.run(runtime.run(settings, session_factory(engine)))
    except KeyboardInterrupt:
        log.info("shutdown_requested")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())

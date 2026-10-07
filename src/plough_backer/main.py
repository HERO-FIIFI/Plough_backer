"""Entry point: `python -m plough_backer.main` (README §74).

§30 restart sequence: 1-2 SQLite + migration gate and 5 config (bootstrap), then
runtime.run(): 3 Telegram, 4 MT5, 6-10 recovery + reconciliation, 11 resume monitoring.
Exits non-zero before touching Telegram/MT5 if any open trading decision is unset.
"""

import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable

from sqlalchemy import Engine

from plough_backer import SPEC_VERSION, runtime
from plough_backer.config import (
    Settings,
    load_account_environment,
    load_accounts,
    load_settings,
    load_sources,
    load_symbols,
    validate_broker_configuration,
)
from plough_backer.exceptions import ConfigurationError, MigrationStateError
from plough_backer.logging import configure_logging
from plough_backer.persistence.database import (
    assert_migrations_current,
    make_engine,
    session_factory,
)

log = logging.getLogger(__name__)

EXIT_CONFIG_ERROR = 2
RESTART_MIN_DELAY_S = 5
RESTART_MAX_DELAY_S = 300
HEALTHY_RUN_S = 600  # a run this long resets the backoff


def serve(
    run_once: Callable[[], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Restart the runtime forever with exponential backoff; only Ctrl+C stops it.

    Safe because every run repeats the §30 restart sequence (recovery, reconciliation,
    catch-up). A process crash still needs an OS supervisor (NSSM / Task Scheduler).
    """
    delay = RESTART_MIN_DELAY_S
    while True:
        started = clock()
        try:
            run_once()
            log.warning("runtime_stopped")  # Telegram disconnected for good
        except Exception:
            log.exception("runtime_crashed")
        if clock() - started >= HEALTHY_RUN_S:
            delay = RESTART_MIN_DELAY_S
        log.info("runtime_restarting", extra={"delay_s": delay})
        sleep(delay)
        delay = min(delay * 2, RESTART_MAX_DELAY_S)


def bootstrap(settings: Settings) -> Engine:
    """Validate config + database. Raises ConfigurationError / MigrationStateError."""
    symbols = load_symbols()
    sources = load_sources()
    accounts = load_accounts()
    account_environment = load_account_environment(os.environ)
    account_credentials = validate_broker_configuration(
        settings, accounts, account_environment
    )
    account_secrets = [item.password.get_secret_value() for item in account_credentials]
    configure_logging(settings.log_level, [*settings.secret_values(), *account_secrets])
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
            "master_accounts": len(accounts.enabled_masters),
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
    if load_accounts().enabled_masters:
        missing = [name for name in missing if name not in {"MT5_DEVIATION_POINTS", "MT5_MAGIC"}]
    if missing:  # README §76: open trading decisions are never defaulted
        engine.dispose()
        print(f"startup refused: set {', '.join(missing)} in .env", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    sessions = session_factory(engine)
    # Telethon receive callbacks don't fire on Windows' default Proactor loop.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        serve(lambda: asyncio.run(runtime.run(settings, sessions), loop_factory=loop_factory))
    except KeyboardInterrupt:
        log.info("shutdown_requested")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())

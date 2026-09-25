"""Runtime controls persisted in `settings` (§32–§35, §39) — every change is audited (§54).

Env values only seed an empty DB; values changed from Telegram then win across restarts
(Q-17, provisional).
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from plough_backer.config import Settings
from plough_backer.enums import AuditEventType, RiskMode, TradingStatus
from plough_backer.persistence import repositories as repo
from plough_backer.persistence.models import ProgressionStateRow
from plough_backer.risk.progression import ProgressionState

RISK_MODE, EQUITY_LOCK, TRADING_STATUS = "risk_mode", "equity_lock", "trading_status"


def seed_from_env(session: Session, settings: Settings) -> None:
    repo.seed_setting(session, RISK_MODE, {"mode": int(settings.default_risk_mode)})
    repo.seed_setting(
        session,
        EQUITY_LOCK,
        {"enabled": settings.equity_lock_enabled, "value": settings.equity_lock_value},
    )
    repo.seed_setting(session, TRADING_STATUS, {"status": TradingStatus.RUNNING})


def _get(session: Session, key: str) -> dict[str, object]:
    value = repo.get_setting(session, key)
    if value is None:
        raise RuntimeError(f"setting {key!r} missing — seed_from_env was not run")
    return value


def risk_mode(session: Session) -> RiskMode:
    return RiskMode(int(str(_get(session, RISK_MODE)["mode"])))


def equity_lock(session: Session) -> tuple[bool, Decimal | None]:
    v = _get(session, EQUITY_LOCK)
    raw = v.get("value")
    return bool(v["enabled"]), None if raw is None else Decimal(str(raw))


def trading_status(session: Session) -> TradingStatus:
    return TradingStatus(str(_get(session, TRADING_STATUS)["status"]))


def set_trading_status(session: Session, status: TradingStatus, *, actor: str) -> None:
    old = trading_status(session)
    repo.set_setting(session, TRADING_STATUS, {"status": status}, updated_by=actor)
    event = {
        TradingStatus.PAUSED: AuditEventType.PAUSED,
        TradingStatus.RUNNING: AuditEventType.RESUMED,
        TradingStatus.STOPPED: AuditEventType.KILL_SWITCH,
    }[status]
    repo.append_audit(session, event, actor=actor, old_status=old, new_status=status)


def set_equity_lock(session: Session, *, enabled: bool, value: Decimal | None, actor: str) -> None:
    if enabled and (value is None or not value.is_finite() or value < 0):
        raise ValueError("an enabled Equity Lock needs a non-negative amount")
    old_enabled, old_value = equity_lock(session)
    repo.set_setting(session, EQUITY_LOCK, {"enabled": enabled, "value": value}, updated_by=actor)
    repo.append_audit(
        session,
        AuditEventType.EQUITY_LOCK_CHANGED,
        actor=actor,
        old_enabled=old_enabled,
        old_value=old_value,
        new_enabled=enabled,
        new_value=value,
    )


def change_risk_mode(session: Session, new_mode: RiskMode, *, actor: str) -> None:
    """§33: the new method starts from base state in every scope; old lots are recorded."""
    old_mode = risk_mode(session)
    repo.set_setting(session, RISK_MODE, {"mode": int(new_mode)}, updated_by=actor)
    resets = []
    for row in session.scalars(select(ProgressionStateRow)).all():
        fresh = ProgressionState.initial(new_mode, row.base_lot)
        resets.append(
            {
                "scope": row.scope_key,
                "old_theoretical_lot": row.theoretical_lot,
                "new_theoretical_lot": fresh.theoretical_lot,
            }
        )
        repo.save_progression(session, row.scope_key, fresh, expected_version=row.version)
    repo.append_audit(
        session,
        AuditEventType.MODE_CHANGED,
        actor=actor,
        old_mode=int(old_mode),
        new_mode=int(new_mode),
        scopes=resets,
    )

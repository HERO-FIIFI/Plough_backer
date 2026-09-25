import logging
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Column, Integer, MetaData, Table, insert, select, text
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Mapped, mapped_column

from plough_backer.config import load_settings
from plough_backer.exceptions import MigrationStateError
from plough_backer.main import EXIT_CONFIG_ERROR, bootstrap, main
from plough_backer.persistence.database import (
    DecimalText,
    alembic_config,
    assert_migrations_current,
    make_engine,
)
from plough_backer.persistence.models import Base
from tests.conftest import paper_settings


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'nested' / 'pb.db').as_posix()}"


def _upgrade(url: str) -> None:
    cfg = alembic_config(url)
    cfg.attributes["configure_logger"] = False  # keep pytest's log capture intact
    command.upgrade(cfg, "head")


def test_fresh_database_is_refused_until_migrated(db_url: str) -> None:
    engine = make_engine(db_url)  # also creates the missing parent directory
    with pytest.raises(MigrationStateError, match="no revision"):
        assert_migrations_current(engine)
    _upgrade(db_url)
    assert_migrations_current(engine)
    engine.dispose()


def test_startup_does_not_create_schema(db_url: str) -> None:
    engine = make_engine(db_url)
    with pytest.raises(MigrationStateError):
        assert_migrations_current(engine)
    with engine.connect() as conn:
        tables = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()
    assert tables == []
    engine.dispose()


def test_sqlite_pragmas_applied(db_url: str) -> None:
    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 2  # FULL
    engine.dispose()


def test_decimal_round_trip_is_exact_and_floats_are_refused(db_url: str) -> None:
    md = MetaData()
    t = Table("lots", md, Column("id", Integer, primary_key=True), Column("lot", DecimalText()))
    engine = make_engine(db_url)
    md.create_all(engine)  # test-only table, not application schema
    values = [Decimal("0.050625"), Decimal("0.010000"), Decimal("0.033750"), Decimal("1E-30")]
    with engine.begin() as conn:
        conn.execute(insert(t), [{"lot": v} for v in values])
        stored = conn.execute(select(t.c.lot).order_by(t.c.id)).scalars().all()
        assert [str(v) for v in stored] == [str(v) for v in values]
        with pytest.raises(StatementError):
            conn.execute(insert(t), {"lot": 0.05})
    engine.dispose()


def test_orm_decimal_annotations_map_to_exact_text() -> None:
    class Probe(Base):
        __tablename__ = "probe_only_in_test"
        id: Mapped[int] = mapped_column(primary_key=True)
        lot: Mapped[Decimal]

    assert isinstance(Probe.__table__.c.lot.type, DecimalText)
    Base.metadata.remove(Probe.__table__)  # type: ignore[arg-type]


def test_bootstrap_succeeds_on_migrated_database(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(Path(__file__).parents[2])  # config/*.yaml are repo-relative
    _upgrade(db_url)
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:  # bootstrap installs the JSON handler; don't leak it into other tests
        settings = load_settings(_env_file=None, **paper_settings(database_url=db_url))
        bootstrap(settings).dispose()
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_main_refuses_to_start_without_configuration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main() == EXIT_CONFIG_ERROR
    assert "execution_mode" in capsys.readouterr().err

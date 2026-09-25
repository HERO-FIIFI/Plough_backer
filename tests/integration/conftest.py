"""Shared fixtures: a migrated SQLite DB seeded with runtime controls, and a fake broker.

test_persistence.py overrides db_url/engine with an unseeded variant.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine

from plough_backer import controls
from plough_backer.config import load_settings
from plough_backer.persistence.database import alembic_config, make_engine, session_factory
from tests.conftest import paper_settings
from tests.fakes import FakeGateway


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{(tmp_path / 'pb.db').as_posix()}"
    cfg = alembic_config(url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    e = make_engine(db_url)
    with session_factory(e).begin() as s:
        controls.seed_from_env(s, load_settings(_env_file=None, **paper_settings()))
    yield e
    e.dispose()


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()

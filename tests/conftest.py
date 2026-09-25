import os
from pathlib import Path
from typing import Any

import pytest

from plough_backer.config import Settings


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-mt5-demo",
        action="store_true",
        help="run tests that need a real MT5 demo terminal",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-mt5-demo"):
        return
    skip = pytest.mark.skip(reason="needs --run-mt5-demo and a real MT5 demo terminal")
    for item in items:
        if "mt5_demo" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tests never see the developer's real environment, .env or data/ directory."""
    fields = {f.upper() for f in Settings.model_fields}
    for name in list(os.environ):
        if name.upper() in fields:
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)  # relative ".env" and "data/" resolve to an empty dir


def paper_settings(**overrides: Any) -> dict[str, Any]:
    """Minimal valid PAPER configuration as keyword arguments."""
    return {
        "execution_mode": "PAPER",
        "default_risk_mode": 1,
        "equity_lock_enabled": False,
        **overrides,
    }

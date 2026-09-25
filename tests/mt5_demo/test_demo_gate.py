"""Read-only checks against a REAL MT5 demo terminal (Windows only).

Run on the MT5 machine: pytest tests/mt5_demo --run-mt5-demo
Uses .env credentials; places NO orders. Order scenarios are exercised through the app
itself and evidenced by scripts/demo_acceptance.py (README §68).
"""

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from plough_backer.config import load_settings, load_symbols
from plough_backer.enums import AssetClass, Direction, ExecutionMode
from plough_backer.trading.mt5_client import MT5Client, load_mt5

pytestmark = pytest.mark.mt5_demo
ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def client() -> Iterator[MT5Client]:
    settings = load_settings(_env_file=ROOT / ".env")
    assert settings.execution_mode is ExecutionMode.DEMO, "demo gate runs on DEMO only"
    assert settings.mt5_login
    assert settings.mt5_password
    assert settings.mt5_server
    c = MT5Client(
        load_mt5(),
        login=settings.mt5_login,
        password=settings.mt5_password,
        server=settings.mt5_server,
        deviation_points=settings.mt5_deviation_points or 0,
        magic=settings.mt5_magic or 0,
        terminal_path=settings.mt5_terminal_path,
    )
    c.connect()
    yield c
    c.shutdown()


def test_connected_to_demo_account(client: MT5Client) -> None:
    assert client.is_connected()
    account = client.account_snapshot()
    assert account.equity > 0


@pytest.mark.parametrize("asset", [AssetClass.GOLD, AssetClass.SYNTHETIC])
def test_configured_symbols_have_broker_specs(client: MT5Client, asset: AssetClass) -> None:
    mapped = [
        m
        for m in load_symbols(ROOT / "config" / "symbols.yaml").symbols.values()
        if m.asset_class is asset
    ]
    if not mapped:
        pytest.skip(f"no {asset} symbol configured")
    for m in mapped:
        spec = client.symbol_specification(m.mt5_symbol)
        assert spec.volume_min > 0
        assert spec.volume_step > 0
        tick = client.current_tick(m.mt5_symbol)
        assert tick.ask >= tick.bid > 0
        loss = client.calc_profit(
            m.mt5_symbol, Direction.BUY, spec.volume_min, tick.ask, tick.ask - spec.point * 100
        )
        assert loss < Decimal(0)  # broker-native P/L works for this symbol (§18)

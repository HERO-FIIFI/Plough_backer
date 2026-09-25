from decimal import Decimal
from pathlib import Path

import pytest

from plough_backer.config import load_settings, load_sources, load_symbols
from plough_backer.enums import AssetClass, ExecutionMode, ProgressionScope, RiskMode
from plough_backer.exceptions import ConfigurationError
from plough_backer.persistence.database import PROJECT_ROOT
from tests.conftest import paper_settings


def test_minimal_paper_config_loads_with_spec_defaults() -> None:
    s = load_settings(**paper_settings())
    assert s.execution_mode is ExecutionMode.PAPER
    assert s.default_risk_mode is RiskMode.ANTI_MARTINGALE
    assert s.progression_scope is ProgressionScope.PER_SOURCE  # §23
    assert s.report_timezone == "UTC"


@pytest.mark.parametrize("missing", ["execution_mode", "default_risk_mode", "equity_lock_enabled"])
def test_trading_semantics_are_never_defaulted(missing: str) -> None:
    kwargs = paper_settings()
    del kwargs[missing]
    with pytest.raises(ConfigurationError, match=missing):
        load_settings(**kwargs)


def test_live_is_never_inferred_from_env_value_case() -> None:
    with pytest.raises(ConfigurationError):
        load_settings(**paper_settings(execution_mode="live-ish"))


@pytest.mark.parametrize("mode", ["DEMO", "LIVE"])
def test_broker_modes_require_mt5_credentials(mode: str) -> None:
    with pytest.raises(ConfigurationError, match="MT5_LOGIN, MT5_PASSWORD, MT5_SERVER"):
        load_settings(**paper_settings(execution_mode=mode))


def test_config_error_never_echoes_secret_values() -> None:
    with pytest.raises(ConfigurationError) as exc:
        load_settings(**paper_settings(execution_mode="DEMO", mt5_password="hunter2-secret"))
    assert "hunter2-secret" not in str(exc.value)


def test_secrets_are_masked_in_repr() -> None:
    s = load_settings(**paper_settings(telegram_bot_token="123:ABC-token"))
    assert "123:ABC-token" not in repr(s)
    assert s.secret_values() == ["123:ABC-token"]


def test_equity_lock_enabled_requires_value() -> None:
    with pytest.raises(ConfigurationError, match="EQUITY_LOCK_VALUE"):
        load_settings(**paper_settings(equity_lock_enabled=True))


def test_equity_lock_value_is_exact_decimal() -> None:
    s = load_settings(**paper_settings(equity_lock_enabled=True, equity_lock_value="500.10"))
    assert s.equity_lock_value == Decimal("500.10")
    assert isinstance(s.equity_lock_value, Decimal)


def test_env_file_empty_values_mean_unset(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "EXECUTION_MODE=PAPER\nDEFAULT_RISK_MODE=6\nEQUITY_LOCK_ENABLED=false\n"
        "MT5_LOGIN=\nEQUITY_LOCK_VALUE=\n",
        encoding="utf-8",
    )
    s = load_settings(_env_file=env)
    assert s.mt5_login is None
    assert s.default_risk_mode is RiskMode.WIN_HALF_INCREMENT_LOSS_HOLD


def test_example_env_file_is_valid() -> None:
    load_settings(_env_file=PROJECT_ROOT / ".env.example")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("report_timezone", "Mars/Olympus"),
        ("log_level", "LOUD"),
        ("database_url", "postgresql://x"),
    ],
)
def test_invalid_values_rejected(field: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=field):
        load_settings(**paper_settings(**{field: value}))


def test_repo_yaml_configs_are_valid() -> None:
    symbols = load_symbols(PROJECT_ROOT / "config/symbols.yaml")
    assert symbols.symbols["GOLD"].asset_class is AssetClass.GOLD
    assert symbols.gold.take_profit_policy == "TP2"
    load_sources(PROJECT_ROOT / "config/sources.yaml")


def test_alias_mapping_to_two_symbols_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "symbols.yaml"
    path.write_text(
        "symbols:\n"
        "  GOLD: {aliases: [GOLD], mt5_symbol: XAUUSD, asset_class: GOLD}\n"
        "  OTHER: {aliases: [gold], mt5_symbol: X, asset_class: FOREX_CFD}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="maps to both"):
        load_symbols(path)


def test_unknown_yaml_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "symbols.yaml"
    path.write_text(
        "symbols:\n  GOLD: {aliases: [GOLD], mt5_symbol: X, asset_class: GOLD, lot: 0.01}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_symbols(path)

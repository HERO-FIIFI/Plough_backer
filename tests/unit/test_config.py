from decimal import Decimal
from pathlib import Path

import pytest

from plough_backer.config import (
    load_account_environment,
    load_accounts,
    load_settings,
    load_sources,
    load_symbols,
    resolve_account_credentials,
    validate_broker_configuration,
)
from plough_backer.enums import AccountRole, AssetClass, ExecutionMode, ProgressionScope, RiskMode
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
    settings = load_settings(**paper_settings(execution_mode=mode))
    accounts = load_accounts(PROJECT_ROOT / "config/accounts.yaml")
    with pytest.raises(ConfigurationError, match="enabled accounts or MT5_LOGIN"):
        validate_broker_configuration(settings, accounts, {})


def test_config_error_never_echoes_secret_values() -> None:
    settings = load_settings(
        **paper_settings(execution_mode="DEMO", mt5_password="hunter2-secret")
    )
    accounts = load_accounts(PROJECT_ROOT / "config/accounts.yaml")
    with pytest.raises(ConfigurationError) as exc:
        validate_broker_configuration(settings, accounts, {})
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


def test_method_accepts_one_or_multiple_master_accounts(tmp_path: Path) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text(
        "accounts:\n"
        "  - {id: m1a, name: M1 A, method: 1, role: MASTER, enabled: true, "
        "terminal_path: 'C:\\\\MT5\\\\m1a\\\\terminal64.exe', credentials_prefix: MT5_M1A, "
        "magic: 101, deviation_points: 20}\n"
        "  - {id: m1b, name: M1 B, method: 1, role: MASTER, enabled: true, "
        "terminal_path: 'C:\\\\MT5\\\\m1b\\\\terminal64.exe', credentials_prefix: MT5_M1B, "
        "magic: 102, deviation_points: 20}\n"
        "  - {id: m2, name: M2, method: 2, role: MASTER, enabled: true, "
        "terminal_path: 'C:\\\\MT5\\\\m2\\\\terminal64.exe', credentials_prefix: MT5_M2, "
        "magic: 201, deviation_points: 20}\n",
        encoding="utf-8",
    )

    accounts = load_accounts(path)

    assert [a.id for a in accounts.masters_for(RiskMode.ANTI_MARTINGALE)] == ["m1a", "m1b"]
    assert [a.id for a in accounts.masters_for(RiskMode.MARTINGALE)] == ["m2"]


def test_copier_follower_must_reference_master_using_same_method(tmp_path: Path) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text(
        "accounts:\n"
        "  - {id: master, name: Master, method: 1, role: MASTER, enabled: true, "
        "terminal_path: 'C:\\\\MT5\\\\master\\\\terminal64.exe', "
        "credentials_prefix: MT5_MASTER, magic: 101, deviation_points: 20}\n"
        "  - {id: follower, name: Follower, method: 2, role: COPIER_FOLLOWER, enabled: true, "
        "master_account_id: master, copier_provider: pending, external_reference: ext-1}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="same method"):
        load_accounts(path)


def test_enabled_masters_cannot_share_terminal_or_magic(tmp_path: Path) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text(
        "accounts:\n"
        "  - {id: one, name: One, method: 1, role: MASTER, enabled: true, "
        "terminal_path: 'C:\\\\MT5\\\\same\\\\terminal64.exe', credentials_prefix: MT5_ONE, "
        "magic: 101, deviation_points: 20}\n"
        "  - {id: two, name: Two, method: 2, role: MASTER, enabled: true, "
        "terminal_path: 'c:\\\\mt5\\\\SAME\\\\terminal64.exe', credentials_prefix: MT5_TWO, "
        "magic: 101, deviation_points: 20}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=r"terminal_path|magic"):
        load_accounts(path)


def test_account_credentials_are_resolved_from_prefixed_environment(tmp_path: Path) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text(
        "accounts:\n"
        "  - {id: m1, name: M1, method: 1, role: MASTER, enabled: true, "
        "terminal_path: 'C:\\\\MT5\\\\m1\\\\terminal64.exe', credentials_prefix: MT5_M1, "
        "magic: 101, deviation_points: 20}\n",
        encoding="utf-8",
    )
    account = load_accounts(path).accounts[0]
    credentials = resolve_account_credentials(
        account,
        {"MT5_M1_LOGIN": "123", "MT5_M1_PASSWORD": "secret", "MT5_M1_SERVER": "Demo"},
    )

    assert account.role is AccountRole.MASTER
    assert credentials.login == 123
    assert credentials.server == "Demo"
    assert credentials.password.get_secret_value() == "secret"


def test_dynamic_account_credentials_load_from_dotenv_with_environment_override(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MT5_M1_LOGIN=123\n"
        "MT5_M1_PASSWORD=from-file\n"
        "MT5_M1_SERVER=File-Demo\n",
        encoding="utf-8",
    )

    values = load_account_environment({"MT5_M1_PASSWORD": "from-environment"}, env_file)

    assert values == {
        "MT5_M1_LOGIN": "123",
        "MT5_M1_PASSWORD": "from-environment",
        "MT5_M1_SERVER": "File-Demo",
    }


def test_account_credential_error_does_not_echo_secret(tmp_path: Path) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text(
        "accounts:\n"
        "  - {id: m1, name: M1, method: 1, role: MASTER, enabled: true, "
        "terminal_path: 'C:\\\\MT5\\\\m1\\\\terminal64.exe', credentials_prefix: MT5_M1, "
        "magic: 101, deviation_points: 20}\n",
        encoding="utf-8",
    )
    account = load_accounts(path).accounts[0]
    with pytest.raises(ConfigurationError) as exc:
        resolve_account_credentials(
            account,
            {"MT5_M1_LOGIN": "not-a-number", "MT5_M1_PASSWORD": "never-echo-me"},
        )
    assert "never-echo-me" not in str(exc.value)

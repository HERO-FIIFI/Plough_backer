"""Typed configuration (README §53).

Secrets and runtime switches come from the environment / .env (`Settings`).
Symbol aliases and Telegram sources come from YAML under config/ (`load_symbols`, `load_sources`).

Settings that decide trading semantics (execution mode, risk mode, Equity Lock) have no
defaults: they must be stated, never inferred (README §6, §76).
"""

from decimal import Decimal
from pathlib import Path
from typing import Any, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from plough_backer.enums import (
    AssetClass,
    ExecutionMode,
    MissingTp2Policy,
    ProgressionScope,
    RiskEntrySource,
    RiskMode,
    TakeProfitPolicy,
    VolumeMaxPolicy,
)
from plough_backer.exceptions import ConfigurationError


class DatabaseSettings(BaseSettings):
    """Just the DB URL — lets Alembic run without the trading settings being configured."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # `MT5_LOGIN=` in .env means "unset", not ""
        extra="ignore",
        frozen=True,
    )

    database_url: str = "sqlite:///data/plough_backer.db"

    @field_validator("database_url")
    @classmethod
    def _sqlite_only(cls, value: str) -> str:
        if not value.startswith("sqlite"):  # §4: SQLite for V1
            raise ValueError("V1 supports SQLite DATABASE_URL only")
        return value


class Settings(DatabaseSettings):
    app_env: str = "demo"
    execution_mode: ExecutionMode

    telegram_api_id: int | None = None
    telegram_api_hash: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_admin_user_id: int | None = None

    mt5_login: int | None = None
    mt5_password: SecretStr | None = None
    mt5_server: str | None = None

    default_risk_mode: RiskMode
    progression_scope: ProgressionScope = ProgressionScope.PER_SOURCE  # §23 recommended default

    equity_lock_enabled: bool
    equity_lock_value: Decimal | None = None

    report_timezone: str = "UTC"
    log_level: str = "INFO"

    # --- Runtime choices the README leaves open. Optional here so tooling/tests can load
    # settings, but the live runtime refuses to start without them (runtime_missing()).
    volume_max_policy: VolumeMaxPolicy | None = None  # Q-05
    risk_entry_source: RiskEntrySource | None = None  # Q-06
    breakeven_tolerance: Decimal | None = None  # Q-03
    manual_close_is_other: bool | None = None  # Q-03
    mt5_deviation_points: int | None = None
    mt5_magic: int | None = None
    mt5_terminal_path: str | None = None
    telegram_session_path: str = "data/telegram"
    reconcile_interval_seconds: int = Field(default=30, ge=5)

    def runtime_missing(self) -> list[str]:
        """Settings the live runtime needs that are unset. Never defaulted (§76)."""
        needed = [
            "telegram_api_id",
            "telegram_api_hash",
            "telegram_bot_token",
            "telegram_admin_user_id",
            "volume_max_policy",
            "risk_entry_source",
            "breakeven_tolerance",
            "manual_close_is_other",
        ]
        if self.execution_mode is not ExecutionMode.PAPER:
            needed += ["mt5_deviation_points", "mt5_magic"]
        return [n.upper() for n in needed if getattr(self, n) is None]

    @field_validator("report_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:  # §55
            raise ValueError(f"invalid log level {value!r}")
        return value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Self:
        if self.execution_mode in (ExecutionMode.DEMO, ExecutionMode.LIVE):
            missing = [
                name
                for name in ("mt5_login", "mt5_password", "mt5_server")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    f"EXECUTION_MODE={self.execution_mode} requires {', '.join(missing).upper()}"
                )
        if self.equity_lock_enabled:
            if self.equity_lock_value is None:
                raise ValueError("EQUITY_LOCK_ENABLED=true requires EQUITY_LOCK_VALUE")
            if not self.equity_lock_value.is_finite() or self.equity_lock_value < 0:
                raise ValueError("EQUITY_LOCK_VALUE must be a finite, non-negative amount")
        return self

    def secret_values(self) -> list[str]:
        """Plain secret strings, for log redaction (README §54)."""
        secrets = [self.telegram_api_hash, self.telegram_bot_token, self.mt5_password]
        return [s.get_secret_value() for s in secrets if s is not None and s.get_secret_value()]


def load_settings(**overrides: Any) -> Settings:
    """Load settings, converting validation failures into ConfigurationError.

    The error message lists field names and problems only — never input values, which
    may be secrets.
    """
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}"
            for err in exc.errors(include_input=False)
        )
        raise ConfigurationError(f"invalid configuration: {problems}") from None


# --- YAML configuration -------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SymbolMapping(_Strict):
    """README §11. `mt5_symbol` is broker-specific configuration, never code."""

    aliases: list[str] = Field(min_length=1)
    mt5_symbol: str = Field(min_length=1)
    asset_class: AssetClass
    # Non-Gold only. Unset -> signals for this symbol are rejected (Q-04). Gold uses `gold:`.
    take_profit_policy: TakeProfitPolicy | None = None

    @model_validator(mode="after")
    def _gold_policy_lives_in_gold_section(self) -> Self:
        if self.asset_class is AssetClass.GOLD and self.take_profit_policy is not None:
            raise ValueError("Gold TP policy is set under `gold:`, not per symbol")
        return self


class GoldPolicy(_Strict):
    """README §12."""

    take_profit_policy: TakeProfitPolicy = TakeProfitPolicy.TP2
    missing_tp2_policy: MissingTp2Policy = MissingTp2Policy.REJECT


class SymbolsConfig(_Strict):
    symbols: dict[str, SymbolMapping]
    gold: GoldPolicy = GoldPolicy()

    @model_validator(mode="after")
    def _aliases_unambiguous(self) -> Self:
        # One alias resolving to two symbols would make resolution non-deterministic (§2.1).
        # Compared case-insensitively so the check holds whichever matching rule Phase 4 picks.
        owner: dict[str, str] = {}
        for name, mapping in self.symbols.items():
            for alias in mapping.aliases:
                key = alias.strip().casefold()
                if key in owner and owner[key] != name:
                    raise ValueError(f"alias {alias!r} maps to both {owner[key]} and {name}")
                owner[key] = name
        return self


class SourceConfig(_Strict):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    telegram_chat_id: int
    parser_profile: str = Field(min_length=1)
    enabled: bool = True


class SourcesConfig(_Strict):
    sources: list[SourceConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        for attr in ("id", "telegram_chat_id"):
            values = [getattr(s, attr) for s in self.sources]
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate source {attr}")
        return self


def _load_yaml[M: BaseModel](path: Path, model: type[M]) -> M:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return model.model_validate(raw or {})
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(f"invalid {path}: {exc}") from exc


def load_symbols(path: Path = Path("config/symbols.yaml")) -> SymbolsConfig:
    return _load_yaml(path, SymbolsConfig)


def load_sources(path: Path = Path("config/sources.yaml")) -> SourcesConfig:
    return _load_yaml(path, SourcesConfig)

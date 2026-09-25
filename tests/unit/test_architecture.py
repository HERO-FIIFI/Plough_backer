"""Import smoke test + enforced layer boundaries (trading rules independent of infrastructure)."""

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import plough_backer
from plough_backer.enums import RiskMode

PKG_ROOT = Path(plough_backer.__file__).parent
ALL_MODULES = sorted(
    m.name for m in pkgutil.walk_packages(plough_backer.__path__, prefix="plough_backer.")
)

_INFRA = {
    "telethon",
    "telegram",
    "MetaTrader5",
    "apscheduler",
    "plough_backer.telegram",
    "plough_backer.scheduler",
    "plough_backer.trading.mt5_client",
    "plough_backer.main",
}
_DB = {"sqlalchemy", "alembic", "plough_backer.persistence"}

# Domain layers must be testable with no Telegram, no MT5 and (for pure rules) no database.
FORBIDDEN: dict[str, set[str]] = {
    "signals": _INFRA | _DB,
    "risk": _INFRA | _DB,
    "shadow": _INFRA | _DB,
    "analytics": _INFRA,
    "trading/gateway.py": _INFRA | _DB,
    "enums.py": _INFRA | _DB,
    "decimal_utils.py": _INFRA | _DB,
    "exceptions.py": _INFRA | _DB,
}


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _violates(name: str, forbidden: set[str]) -> bool:
    return any(name == f or name.startswith(f + ".") for f in forbidden)


@pytest.mark.parametrize("module", ALL_MODULES)
def test_every_module_imports(module: str) -> None:
    importlib.import_module(module)


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_boundaries(layer: str) -> None:
    target = PKG_ROOT / layer
    files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
    assert files, f"{layer} has no files"
    bad = [
        f"{f.relative_to(PKG_ROOT)} imports {name}"
        for f in files
        for name in _imports(f)
        if _violates(name, FORBIDDEN[layer])
    ]
    assert not bad, bad


def test_boundary_checker_detects_violations(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("import MetaTrader5 as mt5\nfrom plough_backer.persistence.models import Base\n")
    assert {n for n in _imports(f) if _violates(n, _INFRA | _DB)} == {
        "MetaTrader5",
        "plough_backer.persistence.models",
    }


def test_six_risk_modes_with_labels() -> None:
    assert [int(m) for m in RiskMode] == [1, 2, 3, 4, 5, 6]
    assert all(m.label for m in RiskMode)

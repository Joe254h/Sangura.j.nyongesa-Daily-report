"""Import-rule enforcement (§2.1, §12.6).

Every module's AST is parsed and its imports checked against the layer table. This is
cheap and it catches architectural drift immediately: ``strategy/`` importing
``MetaTrader5`` fails CI, and so does ``backtrader`` anywhere outside ``backtest/``.

``TYPE_CHECKING``-guarded imports are ignored on purpose. ``core/models.py`` annotates
``StrategyContext.params`` with ``config.schema.StrategyParams`` and ``config/`` imports
``core``; the annotation is a string at runtime and creates no dependency. The rule being
enforced is "what does this module import when it runs", which is the rule the arrows in
§2 are about.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "fxbot"

FORBIDDEN: dict[str, tuple[str, ...]] = {
    "core": ("fxbot.config", "fxbot.indicators", "fxbot.strategy", "fxbot.risk", "fxbot.data",
             "fxbot.execution", "fxbot.runtime", "fxbot.backtest", "fxbot.ops",
             "backtrader", "MetaTrader5"),
    "indicators": ("fxbot.config", "fxbot.strategy", "fxbot.risk", "fxbot.data",
                   "fxbot.execution", "fxbot.runtime", "fxbot.backtest", "fxbot.ops",
                   "backtrader", "MetaTrader5"),
    "config": ("fxbot.indicators", "fxbot.strategy", "fxbot.risk", "fxbot.data",
               "fxbot.execution", "fxbot.runtime", "fxbot.backtest", "fxbot.ops",
               "backtrader", "MetaTrader5"),
    "strategy": ("fxbot.data", "fxbot.execution", "fxbot.risk.governor", "fxbot.runtime",
                 "fxbot.backtest", "backtrader", "MetaTrader5"),
    "risk": ("fxbot.strategy", "fxbot.data", "fxbot.execution", "fxbot.runtime",
             "fxbot.backtest", "backtrader", "MetaTrader5"),
    "data": ("fxbot.strategy", "fxbot.risk", "fxbot.execution", "fxbot.runtime",
             "fxbot.backtest", "backtrader"),
    "execution": ("fxbot.strategy", "fxbot.data.quality", "fxbot.data.resample",
                  "fxbot.runtime", "backtrader"),
    "runtime": ("backtrader",),
    "backtest": ("fxbot.execution.mt5_broker", "fxbot.runtime.engine", "MetaTrader5"),
    "ops": ("fxbot.strategy", "fxbot.risk", "fxbot.data", "fxbot.execution",
            "fxbot.runtime", "fxbot.backtest", "backtrader", "MetaTrader5"),
}
"""Package -> module prefixes it may not import at runtime."""

ALLOWED_EDGES: dict[str, tuple[str, ...]] = {
    "fxbot/execution/paper_broker.py": ("fxbot.backtest.costs",),
    "fxbot/runtime/engine.py": ("fxbot.backtest.metrics", "fxbot.backtest.replay"),
}
"""The documented exceptions to the dependency direction (§2.1, §12.5).

``PaperBroker`` imports the shared fill model, because both engines must price fills with
one implementation or ``test_parity.py`` can never pass.

``runtime/engine.py`` imports the replay source and the report builder so
``replay_history`` can drive the live engine over a fixed history -- the other half of the
parity contract. §2.1 lets ``runtime/`` import everything except ``backtrader``, and
:func:`test_the_live_engine_never_pulls_backtrader_in` proves the transitive closure stays
clean. Both edges are asserted rather than ignored, so they stay visible.
"""

# `risk/sizing.py` and `risk/exposure.py` must additionally stay free of the filesystem.
PURE_RISK_MODULES = ("fxbot/risk/sizing.py", "fxbot/risk/exposure.py")
IO_MODULES = ("os", "pathlib", "json", "sqlite3", "socket", "httpx", "requests", "shutil",
              "tempfile")

BANNED_IN_PURE = ("fxbot/core", "fxbot/indicators", "fxbot/strategy", "fxbot/risk/sizing.py",
                  "fxbot/risk/exposure.py")
"""Layers that may not read a wall clock (§0.2, §0.6)."""


def modules() -> list[Path]:
    """Return every Python module in the package."""
    return sorted(SRC.rglob("*.py"))


def imports_of(path: Path) -> list[str]:
    """Return the modules ``path`` imports at runtime, ignoring TYPE_CHECKING blocks."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for child in ast.walk(node):
                guarded.add(id(child))
    found: list[str] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
    return found


def _is_type_checking(test: ast.expr) -> bool:
    """Return whether an ``if`` test is a ``TYPE_CHECKING`` guard."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def relative(path: Path) -> str:
    """Return the module path relative to ``src/``."""
    return str(path.relative_to(SRC.parent)).replace("\\", "/")


@pytest.mark.parametrize("path", modules(), ids=relative)
def test_layer_imports_obey_the_table(path: Path) -> None:
    """No module imports anything its layer forbids (§2.1)."""
    package = path.relative_to(SRC).parts[0] if path.parent != SRC else ""
    forbidden = FORBIDDEN.get(package, ())
    allowed = ALLOWED_EDGES.get(relative(path), ())
    for imported in imports_of(path):
        if any(imported == ok or imported.startswith(ok + ".") for ok in allowed):
            continue
        for banned in forbidden:
            assert not (imported == banned or imported.startswith(banned + ".")), (
                f"{relative(path)} imports {imported}, which its layer forbids")


def test_backtrader_is_confined_to_the_backtest_package() -> None:
    """§17.15: ``backtrader`` imported anywhere else is a defect."""
    offenders = [relative(p) for p in modules()
                 if "backtrader" in imports_of(p) and p.relative_to(SRC).parts[0] != "backtest"]
    assert offenders == []


def test_the_live_engine_never_pulls_backtrader_in() -> None:
    """Importing the live engine must not load ``backtrader``, even transitively.

    The direct-import check above cannot see a two-hop path, and a frozen research
    dependency has no business in the process that sends real orders (§17.15). This
    imports the engine in a clean interpreter and looks at ``sys.modules``.
    """
    import subprocess
    import sys

    probe = ("import sys; import fxbot.runtime.engine; "
             "import fxbot.cli;"  # the CLI is allowed to pull it in lazily, per command
             "sys.exit(1 if 'backtrader' in sys.modules else 0)")
    result = subprocess.run([sys.executable, "-c", probe], check=False, capture_output=True)
    assert result.returncode == 0, (
        "importing runtime.engine loaded backtrader transitively:\n"
        + result.stderr.decode())


def test_metatrader5_is_confined_to_data_and_execution() -> None:
    """Only the adapters touch the terminal; the engine holds a Broker, never MT5."""
    allowed = {"data", "execution"}
    offenders = [relative(p) for p in modules()
                 if "MetaTrader5" in imports_of(p)
                 and (p.parent == SRC or p.relative_to(SRC).parts[0] not in allowed)]
    assert offenders == []


def test_the_paper_broker_edge_is_the_only_exception() -> None:
    """Nothing else outside ``backtest/`` may import ``backtest.costs``."""
    offenders = []
    for path in modules():
        if path.parent == SRC:
            # The composition root (cli.py) sits above every layer and wires them all.
            continue
        if path.relative_to(SRC).parts[0] == "backtest":
            continue
        if relative(path) in ALLOWED_EDGES:
            continue
        if any(i.startswith("fxbot.backtest") for i in imports_of(path)):
            offenders.append(relative(path))
    assert offenders == [], f"undocumented dependency on backtest/: {offenders}"


@pytest.mark.parametrize("module", PURE_RISK_MODULES)
def test_pure_risk_modules_do_no_io(module: str) -> None:
    """``risk/sizing.py`` and ``risk/exposure.py`` import nothing with I/O (§2.1)."""
    path = SRC.parent / module
    for imported in imports_of(path):
        assert imported.split(".")[0] not in IO_MODULES, f"{module} imports {imported}"


def test_the_pure_layers_never_read_a_wall_clock() -> None:
    """``datetime.now()`` / ``time.time()`` in trading logic is a hard ban (§17.4).

    The single true-UTC read lives in ``ops/health.py`` and is injected from there (§0.6).
    """
    offenders: list[str] = []
    for path in modules():
        name = relative(path)
        if not any(name.startswith(prefix) for prefix in BANNED_IN_PURE):
            continue
        source = path.read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                         if not line.strip().startswith("#"))
        for pattern in ("datetime.now(", "time.time(", "datetime.utcnow(", "date.today("):
            if pattern in code:
                offenders.append(f"{name}: {pattern}")
    assert offenders == []


def test_no_bare_except_or_silent_swallow() -> None:
    """§17.18: ``try: ... except Exception: pass`` anywhere in the codebase is a defect."""
    offenders: list[str] = []
    for path in modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                offenders.append(f"{relative(path)}:{node.lineno} bare except")
            body = [n for n in node.body if not isinstance(n, ast.Expr)
                    or not isinstance(n.value, ast.Constant)]
            if body and all(isinstance(n, ast.Pass) for n in body):
                offenders.append(f"{relative(path)}:{node.lineno} silent pass")
    assert offenders == []


def test_no_print_statements_in_the_package() -> None:
    """§16.4: no ``print``. Structured logging only."""
    offenders: list[str] = []
    for path in modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "print"):
                offenders.append(f"{relative(path)}:{node.lineno}")
    assert offenders == []


def test_no_mutable_default_arguments() -> None:
    """§16.4: a mutable default is shared state hiding in a signature."""
    offenders: list[str] = []
    for path in modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    offenders.append(f"{relative(path)}:{node.lineno} {node.name}")
    assert offenders == []

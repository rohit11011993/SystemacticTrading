"""Plugin discovery and validation (FR-5.1, FR-5.3, FR-5.5, FR-13.5, FR-15.2, FR-15.3).

Plugins are plain ``.py`` files in a folder *outside* the executable. At startup each file is:

1. hashed (SHA-256) so a changed plugin must be re-approved before it can trade live;
2. statically scanned with ``ast`` - imports must be on the allow-list, and dangerous
   builtins (``open``, ``exec``, ``eval``, ``__import__``, ``compile``) are refused;
3. imported, and every ``Strategy`` subclass validated against the contract.

The static scan is a guard against accidental coupling and obvious misuse, not a security
sandbox; plugins are still reviewed code from the operator.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from .base import Strategy

log = logging.getLogger(__name__)

# Top-level modules a plugin may import.
ALLOWED_MODULES = {
    "__future__", "math", "statistics", "dataclasses", "typing", "enum", "datetime",
    "collections", "functools", "itertools", "numpy", "pandas", "pydantic",
}
# AlgoTrader modules a plugin may import (everything else - broker, risk, execution - is banned).
ALLOWED_INTERNAL = {"algotrader.strategy.api", "algotrader.indicators"}
FORBIDDEN_CALLS = {"open", "exec", "eval", "__import__", "compile", "globals", "breakpoint"}


class PluginError(Exception):
    pass


@dataclass
class LoadedPlugin:
    path: Path
    sha256: str
    classes: dict[str, type[Strategy]] = field(default_factory=dict)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_source(source: str, filename: str = "<plugin>") -> list[str]:
    """Return a list of policy violations found by static analysis (empty = clean)."""
    problems: list[str] = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _allowed(alias.name):
                    problems.append(f"line {node.lineno}: import '{alias.name}' not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative imports would escape the plugin folder
                problems.append(f"line {node.lineno}: relative import not allowed")
                continue
            mod = node.module or ""
            if mod == "algotrader":
                for alias in node.names:
                    if f"algotrader.{alias.name}" not in ALLOWED_INTERNAL:
                        problems.append(f"line {node.lineno}: 'from algotrader import {alias.name}'"
                                        " not allowed")
            elif not _allowed(mod):
                problems.append(f"line {node.lineno}: import from '{mod}' not allowed")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                problems.append(f"line {node.lineno}: call to '{node.func.id}' not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr not in (
                "__name__", "__init__", "__class__"):
            problems.append(f"line {node.lineno}: dunder attribute '{node.attr}' not allowed")
    return problems


def _allowed(module: str) -> bool:
    if module.startswith("algotrader"):
        return module in ALLOWED_INTERNAL
    return module.split(".")[0] in ALLOWED_MODULES


def validate_class(cls: type[Strategy]) -> list[str]:
    """Check a Strategy subclass against the plugin contract."""
    problems = []
    for attr in ("id", "version", "params_model"):
        if not getattr(cls, attr, None):
            problems.append(f"{cls.__name__}: missing class attribute '{attr}'")
    pm = getattr(cls, "params_model", None)
    if pm is not None and not (inspect.isclass(pm) and issubclass(pm, BaseModel)):
        problems.append(f"{cls.__name__}: params_model must be a pydantic BaseModel")
    if inspect.isabstract(cls):
        problems.append(f"{cls.__name__}: does not implement on_bar")
    if not getattr(cls, "supported_types", None):
        problems.append(f"{cls.__name__}: must declare supported_types (FR-5.7)")
    return problems


def load_plugins(folder: str | Path) -> dict[str, LoadedPlugin]:
    """Discover and validate every plugin; invalid plugins are rejected and logged.

    Returns ``{strategy_id: LoadedPlugin}``.
    """
    folder = Path(folder)
    found: dict[str, LoadedPlugin] = {}
    if not folder.is_dir():
        raise PluginError(f"plugins folder not found: {folder}")
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            plugin = load_plugin_file(path)
        except PluginError as exc:
            log.error("plugin rejected: %s", exc)
            continue
        for sid in plugin.classes:
            if sid in found:
                log.error("duplicate strategy id %s in %s; keeping %s", sid, path, found[sid].path)
                continue
            found[sid] = plugin
    return found


def load_plugin_file(path: Path) -> LoadedPlugin:
    source = path.read_text(encoding="utf-8")
    problems = check_source(source, str(path))
    if problems:
        raise PluginError(f"{path.name}: " + "; ".join(problems))
    mod_name = f"algotrader_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"{path.name}: cannot be imported")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - any plugin failure is a rejection
        raise PluginError(f"{path.name}: import failed: {exc}") from exc
    plugin = LoadedPlugin(path=path, sha256=file_hash(path))
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, Strategy) and obj is not Strategy and obj.__module__ == mod_name:
            problems = validate_class(obj)
            if problems:
                raise PluginError("; ".join(problems))
            plugin.classes[obj.id] = obj
    if not plugin.classes:
        raise PluginError(f"{path.name}: no Strategy subclass found")
    return plugin

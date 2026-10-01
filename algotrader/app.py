"""Application factory: loads configuration and plugins and wires one ``TradingEngine``.

Startup sequence (PRD s.11, s.13, s.15):
  1. Load and validate the configuration folder (FR-7.6); record its hash (FR-7.7).
  2. Load the instrument registry, cost table and event calendar.
  3. Discover plugins from the external plugins folder, validate the contract and the import
     allow-list, and record each file's hash (FR-5.1, FR-5.3, FR-15.3).
  4. Refuse bindings to unsupported instrument types (FR-5.7), live bindings that have not passed
     the promotion gates (FR-16.3), and changed plugins that were not re-approved (FR-15.3).
  5. Build the engine with the paper broker (backtest / paper) or a supplied live broker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .core.audit import AuditLog
from .core.calendar import EventCalendar
from .core.config import AppConfig, load_config, validate_config
from .core.costs import CostTable
from .core.instruments import InstrumentRegistry
from .core.models import Mode
from .core.state import StateStore
from .data.options import ModelOptionChain
from .data.provider import FileDataProvider, MarketDataProvider
from .engine import BoundStrategy, TradingEngine
from .execution.broker import BrokerGateway
from .execution.paper import PaperBroker
from .strategy.loader import load_plugins


class ConfigError(Exception):
    pass


def resolve(base: Path, p: str | None) -> Path | None:
    if p is None:
        return None
    path = Path(p)
    return path if path.is_absolute() else base / path


def build_engine(config_root: str | Path, *, mode: Mode | None = None, data: MarketDataProvider | None = None,
                 data_dir: str | None = None, broker: BrokerGateway | None = None,
                 state_path: str | None = ":memory:", audit_path: str | None = None,
                 pin_costs_today: bool = False, cost_multiplier: float = 1.0, slippage_mult: float = 1.0,
                 only: list[str] | None = None, param_overrides: dict[str, dict[str, Any]] | None = None,
                 cfg: AppConfig | None = None) -> TradingEngine:
    root = Path(config_root).resolve()
    base = root.parent                      # user data folder: config/, plugins/, data/, state/
    cfg = cfg or load_config(root)
    mode = mode or cfg.system.mode
    registry = InstrumentRegistry.from_yaml(root / "instruments.yaml")
    costs = CostTable.from_yaml(root / "costs.yaml")
    if pin_costs_today:
        costs = costs.pinned(None)
    if cost_multiplier != 1.0:
        costs = costs.scaled(cost_multiplier)
    calendar = EventCalendar.from_yaml(root / "events.yaml")

    errors = validate_config(cfg, registry.keys())
    if errors:
        raise ConfigError("invalid configuration:\n  " + "\n  ".join(errors))

    plugins = load_plugins(resolve(base, cfg.system.plugins_dir))
    store = StateStore(state_path if state_path == ":memory:" else str(resolve(base, state_path)))
    audit = AuditLog(resolve(base, audit_path) if audit_path else None)
    approved = store.get("approved_plugins", {}) or {}

    bound: list[BoundStrategy] = []
    for b in cfg.bindings.bindings:
        if not b.enabled or (only and b.strategy not in only):
            continue
        plugin = plugins.get(b.strategy)
        if plugin is None:
            raise ConfigError(f"binding references unknown or rejected strategy '{b.strategy}'")
        cls = plugin.classes[b.strategy]
        for key in b.instruments:
            itype = registry.get(key).type
            if itype not in cls.supported_types:
                raise ConfigError(f"{b.strategy} does not support {itype.value} ({key}) (FR-5.7)")
        if mode is Mode.LIVE:
            if b.stage not in ("live_reduced", "live"):
                raise ConfigError(f"{b.strategy}: stage '{b.stage}' not cleared for live (FR-16.3)")
            if approved.get(b.strategy) != plugin.sha256:
                raise ConfigError(f"{b.strategy}: plugin changed since approval; re-approve (FR-15.3)")
        params = dict(b.params)
        params.update((param_overrides or {}).get(b.strategy, {}))
        bound.append(BoundStrategy(cls(params), b, plugin.sha256))

    data = data or FileDataProvider(resolve(base, data_dir or cfg.system.data_dir))
    needs_chain = any(bs.strategy.data_needs.needs_option_chain for bs in bound)
    chain = ModelOptionChain(data, registry, vix_feed=cfg.portfolio.regime.vix_series) \
        if needs_chain and mode is not Mode.LIVE else None

    holder: dict[str, TradingEngine] = {}
    if broker is None:
        broker = PaperBroker(registry, costs, clock=lambda: holder["e"].now(), slippage_mult=slippage_mult)
    engine = TradingEngine(cfg, registry, costs, calendar, data, broker, bound, store, audit, mode, chain)
    holder["e"] = engine
    audit.record("startup", "system", {
        "mode": mode.value, "config_hash": cfg.config_hash,
        "plugins": {bs.id: bs.plugin_hash for bs in bound}, "strategies": [bs.id for bs in bound]})
    return engine


def approve_plugins(config_root: str | Path, state_path: str, actor: str) -> dict[str, str]:
    """Record the current plugin hashes as approved for live trading (FR-15.3)."""
    root = Path(config_root).resolve()
    cfg = load_config(root)
    plugins = load_plugins(resolve(root.parent, cfg.system.plugins_dir))
    store = StateStore(str(resolve(root.parent, state_path)))
    hashes = {sid: p.sha256 for sid, p in plugins.items()}
    store.set("approved_plugins", hashes)
    AuditLog(resolve(root.parent, cfg.system.audit_log)).record("plugins_approved", actor, hashes)
    return hashes


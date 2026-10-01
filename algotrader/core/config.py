"""Configuration schemas, loader and validator (PRD s.7, FR-7.2, FR-7.6, FR-7.7, NFR-9).

Every limit, rate, cost and schedule is data. The configuration folder contains:

    system.yaml        mode, NAV, paths, rate limits, session times, static IP
    instruments.yaml   instrument registry                      (core.instruments)
    costs.yaml         versioned cost table                     (core.costs)
    events.yaml        event calendar                           (core.calendar)
    risk.yaml          global / asset-class / instrument / strategy limits and risk profiles
    portfolio.yaml     risk shares, families, regime permission table
    killswitch.yaml    drawdown ladder and kill-switch triggers
    bindings.yaml      strategy <-> instrument bindings

Layered limits: global -> asset class -> instrument -> strategy -> binding profile -> trade.
The **strictest value always wins** (``merge_strictest``), so a lower level can only tighten.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import date, time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from .models import Mode


# --------------------------------------------------------------------------------------------
# Strictest-wins merge
# --------------------------------------------------------------------------------------------
def _strictest(key: str, a: Any, b: Any) -> Any:
    """Combine two values for the same limit key, returning the stricter one.

    Conventions (documented so a new limit picks the right semantics by its name):
      * ``max_*``           smaller is stricter
      * ``min_*``           larger is stricter
      * ``require_*``       True is stricter
      * ``allow_*`` / ``naked_short``  False is stricter
      * lists (e.g. ``blocked_windows``)  union
      * dicts               merged recursively
      * anything else       the later (more specific) level wins
    """
    if a is None:
        return b
    if b is None:
        return a
    if isinstance(a, dict) and isinstance(b, dict):
        return merge_strictest(a, b)
    if isinstance(a, list) and isinstance(b, list):
        return sorted(set(a) | set(b), key=str)
    if key.startswith("max_"):
        return min(a, b)
    if key.startswith("min_"):
        return max(a, b)
    if key.startswith("require_"):
        return bool(a) or bool(b)
    if key.startswith("allow_") or key == "naked_short":
        return bool(a) and bool(b)
    return b


def merge_strictest(*levels: dict[str, Any] | None) -> dict[str, Any]:
    """Merge any number of limit dictionaries, strictest value wins key by key."""
    out: dict[str, Any] = {}
    for level in levels:
        if not level:
            continue
        for k, v in level.items():
            out[k] = _strictest(k, out.get(k), copy.deepcopy(v)) if k in out else copy.deepcopy(v)
    return out


def is_loosening(key: str, old: Any, new: Any) -> bool:
    """True when changing ``old`` -> ``new`` relaxes a limit (needs cooling-off, FR-7.4)."""
    if old is None:
        return False
    return _strictest(key, old, new) == old and old != new


# --------------------------------------------------------------------------------------------
# System settings
# --------------------------------------------------------------------------------------------
class SessionWindow(BaseModel):
    open: time = time(9, 15)
    close: time = time(15, 30)


class SystemConfig(BaseModel):
    mode: Mode = Mode.BACKTEST
    capital: float = 5_000_000.0              # starting NAV in rupees
    data_dir: str = "data"
    state_db: str = "state/state.db"
    audit_log: str = "state/audit.jsonl"
    reports_dir: str = "reports"
    plugins_dir: str = "plugins"
    orders_per_second: float = 2.0            # REG-4 ceiling, far below 10
    broker_requests_per_second: float = 8.0   # vendor limit (FR-6.7), re-verify per release
    static_ip: str | None = None              # REG-1; None disables the check (backtest/paper)
    login_deadline: time = time(8, 45)        # FR-6.3 pre-open alert if no session by then
    sessions: dict[str, SessionWindow] = Field(default_factory=lambda: {"NSE": SessionWindow()})
    max_clock_drift_sec: float = 2.0          # FR-11.6
    heartbeat_timeout_sec: float = 30.0       # watchdog
    reconcile_interval_min: int = 60          # FR-11.3
    vol_target_annual: float | None = 0.12    # portfolio overlay (strategy doc s.4)
    vol_overlay_min_scale: float = 0.5
    gateway_dry_run: bool = False             # FR-7.5
    algo_tags: dict[str, str] = Field(default_factory=dict)  # strategy id -> exchange algo ID


# --------------------------------------------------------------------------------------------
# Risk configuration
# --------------------------------------------------------------------------------------------
class RiskConfig(BaseModel):
    """Layered limits. Each level is a free-form dict validated by ``validate_config``."""

    global_limits: dict[str, Any] = Field(default_factory=dict, alias="global")
    asset_class: dict[str, dict[str, Any]] = Field(default_factory=dict)
    instruments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    strategies: dict[str, dict[str, Any]] = Field(default_factory=dict)
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    def resolve_profile(self, name: str | None, _seen: tuple[str, ...] = ()) -> dict[str, Any]:
        """Resolve a profile including ``extends`` inheritance (FR-7.2)."""
        if not name:
            return {}
        if name in _seen:
            raise ValueError(f"risk profile inheritance cycle: {' -> '.join(_seen + (name,))}")
        if name not in self.profiles:
            raise KeyError(f"unknown risk profile '{name}'")
        prof = dict(self.profiles[name])
        parent = prof.pop("extends", None)
        base = self.resolve_profile(parent, _seen + (name,)) if parent else {}
        # A child profile can only tighten its parent: strictest-wins merge.
        return merge_strictest(base, prof)

    def effective_limits(self, strategy_id: str, instrument_key: str, asset_class: str,
                         profile: str | None, trade_overrides: dict[str, Any] | None = None
                         ) -> dict[str, Any]:
        """Effective limits for one order: the strictest value across all levels."""
        return merge_strictest(
            self.global_limits,
            self.asset_class.get(asset_class),
            self.instruments.get(instrument_key),
            self.strategies.get(strategy_id),
            self.resolve_profile(profile),
            trade_overrides,
        )


# --------------------------------------------------------------------------------------------
# Portfolio allocation and regime permissions (strategy doc s.3 and s.10)
# --------------------------------------------------------------------------------------------
class StrategyAllocation(BaseModel):
    risk_share: float                 # share of book risk (0.30 = 30%)
    per_trade_risk_pct: float         # % of NAV, e.g. 0.5
    per_trade_risk_pct_stock: float | None = None  # S2: 0.25% on stock futures
    max_concurrent: int = 4
    max_per_sector: int | None = None
    hard_stop_pct: float = 5.0        # strategy drawdown hard stop, % of NAV
    soft_stop_pct: float | None = None  # default 60% of hard stop (3% vs 5%)
    family: str = "trend"
    high_tail_risk: bool = False      # stopped at the Orange drawdown state (PRD s.9)
    on_red: str = "alert"             # alignment auto-action: alert | reduce | exit


class FamilyCap(BaseModel):
    max_share: float                  # combined share of book risk


class RegimeConfig(BaseModel):
    er_window: int = 10
    er_threshold: float = 0.30        # one threshold shared by every strategy
    vix_lookback: int = 252
    vix_stress_pct: float = 0.80
    confirm_days: int = 2             # hysteresis
    market_series: str = "NIFTY_FUT"
    vix_series: str = "INDIAVIX"
    # regime name -> strategy id -> size multiplier (0 = no new entries)
    permissions: dict[str, dict[str, float]] = Field(default_factory=dict)


class PortfolioConfig(BaseModel):
    book_risk_budget_pct: float = 8.0   # total open risk of the book, % of NAV
    reserve_share: float = 0.05
    strategies: dict[str, StrategyAllocation] = Field(default_factory=dict)
    families: dict[str, FamilyCap] = Field(default_factory=dict)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)


# --------------------------------------------------------------------------------------------
# Kill switches (PRD s.9)
# --------------------------------------------------------------------------------------------
class LadderStep(BaseModel):
    state: str
    drawdown_pct: float               # negative threshold, e.g. -4
    action: str                       # warn | reduce | flatten | halt
    risk_multiplier: float = 1.0
    stop_high_tail: bool = False
    halt_days: int = 0
    require_external_key: bool = False


class StrategyKillConfig(BaseModel):
    max_loss_streak: int = 8
    max_slippage_ratio: float = 1.5
    max_red_share: float = 0.5
    max_rejections: int = 5
    hit_rate_band: tuple[float, float] | None = None   # from backtest confidence band
    min_trades_for_drift: int = 20
    full_loss_suspend_days: int = 0     # S5: one full-loss structure suspends for review
    avg_loss_to_win_max: float | None = None  # S3: avg loss over last 20 > 2x avg win


class KillSwitchConfig(BaseModel):
    daily_loss_pct: float = 1.5
    weekly_loss_pct: float = 3.5
    ladder: list[LadderStep] = Field(default_factory=list)
    strategies: dict[str, StrategyKillConfig] = Field(default_factory=dict)
    instrument_loss_budget_pct: float = 1.0
    instrument_move_sigma: float = 6.0
    escalate_instrument_trips: int = 3      # repeated instrument trips -> strategy trip
    escalate_strategy_trips: int = 2        # repeated strategy trips -> drawdown level
    escalate_window_days: float = 5.0       # rolling window for counting repeated trips
    reset_cooloff_minutes: float = 30.0
    data_auto_reset_minutes: float = 15.0   # only data-health trips may auto-reset
    data_auto_reset_enabled: bool = True
    disconnect_limit_minutes: float = 5.0
    margin_hard_cap_pct: float = 50.0
    external_reset_code_hashes: list[str] = Field(default_factory=list)  # sha256 of one-time codes
    reduce_fraction: float = 0.5
    loosen_cooloff_hours: float = 24.0      # FR-7.4
    # BACKTEST ONLY: simulate the operator's manual reset this many days after a trip (after
    # any halt period), so one trip does not freeze the rest of a research run. BLACK is never
    # reset unless backtest_reset_black is true. Never applies in paper or live mode.
    backtest_auto_reset_days: float | None = 5.0
    backtest_reset_black: bool = False


# --------------------------------------------------------------------------------------------
# Bindings (PRD s.5)
# --------------------------------------------------------------------------------------------
class Binding(BaseModel):
    strategy: str
    instruments: list[str]
    mode: Mode = Mode.BACKTEST
    params: dict[str, Any] = Field(default_factory=dict)
    risk_profile: str | None = None
    capital_cap_pct: float | None = None
    enabled: bool = True
    stage: str = "backtest"   # draft | backtest | paper | live_reduced | live | retired

    @field_validator("instruments", mode="before")
    @classmethod
    def _one_or_many(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v


class BindingsConfig(BaseModel):
    bindings: list[Binding] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------------------------
class AppConfig(BaseModel):
    """Everything loaded from the config folder, plus the folder hash (FR-7.7)."""

    root: str
    system: SystemConfig
    risk: RiskConfig
    portfolio: PortfolioConfig
    killswitch: KillSwitchConfig
    bindings: BindingsConfig
    config_hash: str

    model_config = {"arbitrary_types_allowed": True}

    def path(self, name: str) -> Path:
        return Path(self.root) / name


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def config_hash(root: str | Path) -> str:
    """SHA-256 over every YAML file in the folder; recorded at startup and on change."""
    h = hashlib.sha256()
    for p in sorted(Path(root).glob("*.y*ml")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def load_config(root: str | Path) -> AppConfig:
    root = Path(root)
    return AppConfig(
        root=str(root),
        system=SystemConfig(**_load(root / "system.yaml")),
        risk=RiskConfig(**_load(root / "risk.yaml")),
        portfolio=PortfolioConfig(**_load(root / "portfolio.yaml")),
        killswitch=KillSwitchConfig(**_load(root / "killswitch.yaml")),
        bindings=BindingsConfig(**_load(root / "bindings.yaml")),
        config_hash=config_hash(root),
    )


def validate_config(cfg: AppConfig, known_instruments: list[str] | None = None) -> list[str]:
    """Reject incoherent configuration (FR-7.6). Returns a list of error strings (empty = OK)."""
    errors: list[str] = []
    g = cfg.risk.global_limits
    g_trade = g.get("max_risk_per_trade_pct")

    for sid, lim in cfg.risk.strategies.items():
        s_trade = lim.get("max_risk_per_trade_pct")
        if g_trade is not None and s_trade is not None and s_trade > g_trade:
            errors.append(f"strategy {sid}: per-trade limit {s_trade}% above global {g_trade}%")
    for sid, alloc in cfg.portfolio.strategies.items():
        if g_trade is not None and alloc.per_trade_risk_pct > g_trade:
            errors.append(f"allocation {sid}: per-trade risk {alloc.per_trade_risk_pct}% "
                          f"above global limit {g_trade}%")
        if alloc.soft_stop_pct is not None and alloc.soft_stop_pct >= alloc.hard_stop_pct:
            errors.append(f"allocation {sid}: soft stop must be below hard stop")

    shares = sum(a.risk_share for a in cfg.portfolio.strategies.values()) + cfg.portfolio.reserve_share
    if shares > 1.0 + 1e-9:
        errors.append(f"risk shares plus reserve sum to {shares:.2f} > 1.0")

    for fam, cap in cfg.portfolio.families.items():
        members = [a for a in cfg.portfolio.strategies.values() if a.family == fam]
        if not members:
            errors.append(f"family '{fam}' has no member strategies")
        if cap.max_share > 1.0:
            errors.append(f"family '{fam}' cap above 100% of book risk")

    for name in cfg.risk.profiles:
        try:
            cfg.risk.resolve_profile(name)
        except (KeyError, ValueError) as exc:
            errors.append(str(exc))

    steps = cfg.killswitch.ladder
    if any(steps[i].drawdown_pct <= steps[i + 1].drawdown_pct for i in range(len(steps) - 1)):
        errors.append("drawdown ladder must be strictly decreasing (e.g. -4, -7, -10, -15)")

    for b in cfg.bindings.bindings:
        if b.risk_profile and b.risk_profile not in cfg.risk.profiles:
            errors.append(f"binding {b.strategy}: unknown risk profile '{b.risk_profile}'")
        if b.mode is Mode.LIVE:
            limits = merge_strictest(g, cfg.risk.resolve_profile(b.risk_profile)
                                     if b.risk_profile in cfg.risk.profiles else {})
            if not limits.get("require_stop", False):
                errors.append(f"live binding {b.strategy}: risk profile lacks require_stop")
            if b.stage not in ("live_reduced", "live"):
                errors.append(f"live binding {b.strategy}: stage '{b.stage}' has not passed the "
                              "promotion gates (FR-16.3)")
        if b.strategy not in cfg.portfolio.strategies:
            errors.append(f"binding {b.strategy}: no allocation in portfolio.yaml")
        if known_instruments is not None:
            for key in b.instruments:
                if key not in known_instruments:
                    errors.append(f"binding {b.strategy}: unknown instrument '{key}'")
    return errors


def today() -> date:
    return date.today()

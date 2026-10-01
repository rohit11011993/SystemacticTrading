"""Command-line entry point: ``algotrader <sub-command>`` (PRD s.13 process model).

Sub-commands
------------
  make-synthetic   write synthetic daily data (demo / plumbing only)
  validate-config  run the configuration validator (FR-7.6)
  backtest         run a backtest; optionally at today's cost rates or with cost stress
  run-day          run one session in paper (or live) mode with persistent state
  replay           run paper sessions over a date range with persistent state (demo)
  status           print the active-trade blotter and kill-switch states
  kill             the operator's big red button (system kill switch)
  reset            request / confirm a kill-switch reset (reason + cooling-off + key)
  approve-plugins  record current plugin hashes as approved for live (FR-15.3)
  keygen           generate one-time external reset codes and their hashes
  drill            rehearse every kill-switch level against the paper broker (FR-9.6)
  watchdog         run the heartbeat supervisor
  engine           long-running engine: applies operator commands, publishes the UI snapshot
  ui               PySide6 desktop UI (attaches to the engine through the state database)
  verify-audit     verify the audit log hash chain

The engine, gateway and watchdog roles map to these commands; in version 1 the engine hosts
the gateway in-process (the gateway API is transport-agnostic, see ``risk.gateway``).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import secrets
import sys
from datetime import date, datetime
from pathlib import Path



def _date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def cmd_make_synthetic(a: argparse.Namespace) -> int:
    from .data.synthetic import generate, write_csv
    paths = write_csv(generate(a.start, a.end, a.seed), a.out)
    print(f"wrote {len(paths)} files to {a.out}")
    return 0


def cmd_validate(a: argparse.Namespace) -> int:
    from .core.config import load_config, validate_config
    from .core.instruments import InstrumentRegistry
    cfg = load_config(a.config)
    reg = InstrumentRegistry.from_yaml(Path(a.config) / "instruments.yaml")
    errors = validate_config(cfg, reg.keys())
    print(f"config hash {cfg.config_hash}")
    for e in errors:
        print(f"ERROR: {e}")
    print("configuration OK" if not errors else f"{len(errors)} error(s)")
    return 1 if errors else 0


def cmd_backtest(a: argparse.Namespace) -> int:
    from .app import build_engine
    from .backtest import format_summary, run_backtest, write_report
    only = a.strategies.split(",") if a.strategies else None
    engine = build_engine(a.config, data_dir=a.data, pin_costs_today=a.today_costs,
                          cost_multiplier=a.cost_mult, slippage_mult=a.slippage_mult, only=only,
                          audit_path=a.audit)
    res = run_backtest(engine, _date(a.start), _date(a.end), progress=True)
    print(format_summary(res.summary))
    tag = "backtest" + ("_today_costs" if a.today_costs else "") + (f"_x{a.cost_mult}" if a.cost_mult != 1 else "")
    for p in write_report(res, a.out, tag):
        print(f"wrote {p}")
    return 0


def _engine_for_session(a: argparse.Namespace):
    from .app import build_engine
    from .core.config import load_config
    from .core.models import Mode
    cfg = load_config(a.config)
    mode = Mode(a.mode)
    broker = None
    if mode is Mode.LIVE:  # pragma: no cover - requires broker credentials
        from .core.instruments import InstrumentRegistry
        from .execution.kite import KiteAuth, KiteBroker
        ok, msg = KiteAuth(a.api_key, cfg.system.static_ip).check_static_ip()
        if not ok:
            print(f"REFUSING TO TRADE: {msg} (REG-1)")
            raise SystemExit(2)
        token = KiteAuth.load_token()
        if not token:
            print("no valid broker session for today: complete the daily login first (REG-2)")
            raise SystemExit(2)
        reg = InstrumentRegistry.from_yaml(Path(a.config) / "instruments.yaml")

        def symbol_map(sym: str) -> tuple[str, str]:
            inst = reg.get(sym) if sym in reg else reg.get(sym.split(":")[0])
            return inst.exchange, inst.data_symbol or inst.symbol
        broker = KiteBroker(a.api_key, token, symbol_map)
    engine = build_engine(a.config, mode=mode, broker=broker, data_dir=a.data,
                          state_path=cfg.system.state_db, audit_path=cfg.system.audit_log, cfg=cfg)
    engine.restore_state()
    return engine


def cmd_run_day(a: argparse.Namespace) -> int:
    from .monitoring.blotter import blotter_rows, render_text
    engine = _engine_for_session(a)
    feed = engine.registry.get(engine.cfg.portfolio.regime.market_series).feed
    days = engine.data.trading_days(feed, end=_date(a.date))
    if not days:
        print("no market data for the requested date")
        return 1
    ts = days[-1]
    if engine._last_day and ts.date() <= engine._last_day:
        print(f"session {ts.date()} already processed (last {engine._last_day})")
        return 0
    row = engine.run_day(ts)
    print(f"{ts.date()} equity {row['equity']:,.0f} ladder {row['ladder']} regime {row['regime']}")
    print(render_text(blotter_rows(engine.book.open_trades(), engine.prices(), engine.nav(), engine.now())))
    for msg in dict.fromkeys(engine.alerts):
        print(f"ALERT: {msg}")
    return 0


def cmd_replay(a: argparse.Namespace) -> int:
    """Run consecutive paper sessions over a date range with persistent state, so the engine
    and desktop UI have positions and history to show (demo / rehearsal)."""
    engine = _engine_for_session(a)
    feed = engine.registry.get(engine.cfg.portfolio.regime.market_series).feed
    days = engine.data.trading_days(feed, _date(a.start), _date(a.end))
    if engine._last_day:
        days = [d for d in days if d.date() > engine._last_day]
    for i, ts in enumerate(days):
        row = engine.run_day(ts)
        if i % 20 == 0 or i == len(days) - 1:
            print(f"{ts.date()} equity {row['equity']:,.0f} open trades {row['open_trades']} ladder {row['ladder']}")
    print(f"replayed {len(days)} session(s); start `engine` and `ui` to inspect the result")
    if a.wait:      # keep the console open when launched from a Start Menu shortcut
        input("Press Enter to close...")
    return 0


def cmd_engine(a: argparse.Namespace) -> int:
    """Long-running engine: applies UI/CLI commands every ``--interval`` seconds, publishes the UI
    snapshot and heartbeat, and runs the daily session when a new bar appears in the data."""
    import time as _t
    engine = _engine_for_session(a)
    feed = engine.registry.get(engine.cfg.portfolio.regime.market_series).feed
    engine.publish_snapshot()
    print(f"engine running in {a.mode} mode (Ctrl+C to stop); last session {engine._last_day}")
    last_reload = last_pub = 0.0
    try:
        while True:
            now = _t.time()
            engine.check_external_commands()
            if engine.process_commands():
                last_pub = now
            if now - last_reload >= a.reload_sec:
                engine.reload_data()
                last_reload = now
                days = engine.data.trading_days(feed)
                if days and (engine._last_day is None or days[-1].date() > engine._last_day):
                    row = engine.run_day(days[-1])
                    print(f"{days[-1].date()} equity {row['equity']:,.0f} ladder {row['ladder']}")
                    last_pub = now
            if now - last_pub >= 5:
                engine.publish_snapshot()
                last_pub = now
            engine.store.heartbeat("engine", now)
            if a.once:
                break
            _t.sleep(a.interval)
    except KeyboardInterrupt:
        print("engine stopped; open trades keep their broker-side stops")
    return 0


def cmd_ui(a: argparse.Namespace) -> int:  # pragma: no cover - interactive
    try:
        from .ui.main_window import run_ui
    except ImportError as exc:
        print(f"PySide6 is required for the desktop UI: pip install PySide6 ({exc})")
        return 2
    return run_ui(a.config, a.refresh_ms)


def cmd_status(a: argparse.Namespace) -> int:
    from .core.config import load_config
    from .core.state import StateStore
    from .engine import trade_from_dict
    from .monitoring.blotter import blotter_rows, render_text
    cfg = load_config(a.config)
    store = StateStore(str(Path(a.config).resolve().parent / cfg.system.state_db))
    trades = [trade_from_dict(d) for d in store.load_trades() if d["status"] in ("OPEN", "PENDING")]
    prices = {s: leg.avg_price for t in trades for s, leg in t.legs.items()}
    print(render_text(blotter_rows(trades, prices, cfg.system.capital, datetime.now())))
    print("\nKill switches:")
    for t in store.get("killswitch", []) or []:
        print(f"  {t['level']}:{t['scope']} action={t['action']} reason={t['reason']}")
    lad = store.get("ladder", {}) or {}
    print(f"Drawdown ladder: {lad.get('state', 'GREEN')}")
    return 0


def cmd_kill(a: argparse.Namespace) -> int:
    from .core.audit import AuditLog
    from .core.config import load_config
    from .core.state import StateStore
    cfg = load_config(a.config)
    base = Path(a.config).resolve().parent
    store = StateStore(str(base / cfg.system.state_db))
    store.set("external_kill", {"reason": a.reason or "manual system kill", "actor": "operator",
                                "flatten": a.flatten, "ts": datetime.now().isoformat(), "handled": False})
    AuditLog(base / cfg.system.audit_log).record("manual_kill", "operator", {"flatten": a.flatten,
                                                                               "reason": a.reason})
    print("system kill switch requested: new orders blocked on the next engine cycle"
          + (" and all positions will be flattened" if a.flatten else ""))
    return 0


def cmd_reset(a: argparse.Namespace) -> int:
    from .core.models import Mode
    from .risk.killswitch import KSLevel
    a.mode = Mode.PAPER.value
    engine = _engine_for_session(a)
    lvl = KSLevel(a.level)
    if a.confirm:
        ok, msg = engine.ks.confirm_reset(lvl, a.scope, "operator", a.code, equity=engine.nav())
        print(("RESET: " if ok else "NOT RESET: ") + msg)
    else:
        print(engine.ks.request_reset(lvl, a.scope, a.reason or "", "operator"))
    engine.save_state()
    return 0


def cmd_approve(a: argparse.Namespace) -> int:
    from .app import approve_plugins
    from .core.config import load_config
    cfg = load_config(a.config)
    for sid, h in approve_plugins(a.config, cfg.system.state_db, a.actor).items():
        print(f"approved {sid} {h[:16]}")
    return 0


def cmd_keygen(a: argparse.Namespace) -> int:
    print("Give the CODES to the second key holder; put only the HASHES in killswitch.yaml.")
    for _ in range(a.n):
        code = secrets.token_hex(4).upper()
        print(f"code {code}   sha256 {hashlib.sha256(code.encode()).hexdigest()}")
    return 0


def cmd_drill(a: argparse.Namespace) -> int:
    from .app import build_engine
    from .drill import run_drill
    engine = build_engine(a.config, data_dir=a.data)
    feed = engine.registry.get(engine.cfg.portfolio.regime.market_series).feed
    days = engine.data.trading_days(feed)[-a.days:]
    ok = True
    for r in run_drill(engine, days):
        ok &= r.passed
        print(f"{r.level:<11} {'PASS' if r.passed else 'FAIL'}  blocked={r.blocked} in {r.block_ms:.2f} ms  "
              f"flattened={r.flattened} reset={r.reset_ok}  {r.notes}")
    return 0 if ok else 1


def cmd_watchdog(a: argparse.Namespace) -> int:  # pragma: no cover - long-running
    from .core.audit import AuditLog
    from .core.config import load_config
    from .core.state import StateStore
    from .watchdog import run
    cfg = load_config(a.config)
    base = Path(a.config).resolve().parent
    run(StateStore(str(base / cfg.system.state_db)), AuditLog(base / cfg.system.audit_log),
        cfg.system.heartbeat_timeout_sec)
    return 0


def cmd_verify_audit(a: argparse.Namespace) -> int:
    from .core.audit import AuditLog
    ok, bad = AuditLog(a.path).verify()
    print("audit chain OK" if ok else f"audit chain BROKEN at record {bad}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="algotrader", description=__doc__.split("\n\n")[0])
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("make-synthetic"); s.set_defaults(fn=cmd_make_synthetic)
    s.add_argument("--out", default="data"); s.add_argument("--start", default="2019-01-01")
    s.add_argument("--end", default="2026-09-30"); s.add_argument("--seed", type=int, default=7)

    s = sub.add_parser("validate-config"); s.set_defaults(fn=cmd_validate)
    s.add_argument("--config", default="config")

    s = sub.add_parser("backtest"); s.set_defaults(fn=cmd_backtest)
    s.add_argument("--config", default="config"); s.add_argument("--data")
    s.add_argument("--start"); s.add_argument("--end"); s.add_argument("--out", default="reports")
    s.add_argument("--strategies", help="comma-separated strategy ids")
    s.add_argument("--today-costs", action="store_true", help="price every trade at today's rates")
    s.add_argument("--cost-mult", type=float, default=1.0, help="cost stress multiplier (e.g. 1.5)")
    s.add_argument("--slippage-mult", type=float, default=1.0)
    s.add_argument("--audit", help="write the audit log to this file")

    for name, fn in (("run-day", cmd_run_day), ("reset", cmd_reset)):
        s = sub.add_parser(name); s.set_defaults(fn=fn)
        s.add_argument("--config", default="config"); s.add_argument("--data")
        s.add_argument("--mode", default="paper", choices=["paper", "live"])
        s.add_argument("--api-key", default=None)
        if name == "run-day":
            s.add_argument("--date", help="session date (default: latest in data)")
        else:
            s.add_argument("--level", required=True, choices=["system", "drawdown", "strategy", "instrument"])
            s.add_argument("--scope", required=True); s.add_argument("--reason")
            s.add_argument("--confirm", action="store_true"); s.add_argument("--code")

    s = sub.add_parser("replay", help="run paper sessions over a date range with persistent state")
    s.set_defaults(fn=cmd_replay)
    s.add_argument("--config", default="config"); s.add_argument("--data")
    s.add_argument("--mode", default="paper", choices=["paper"]); s.add_argument("--api-key", default=None)
    s.add_argument("--start", default="2026-01-01"); s.add_argument("--end")
    s.add_argument("--wait", action="store_true", help="wait for Enter before exiting")

    s = sub.add_parser("engine", help="long-running engine that serves the desktop UI")
    s.set_defaults(fn=cmd_engine)
    s.add_argument("--config", default="config"); s.add_argument("--data")
    s.add_argument("--mode", default="paper", choices=["paper", "live"]); s.add_argument("--api-key", default=None)
    s.add_argument("--interval", type=float, default=1.0); s.add_argument("--reload-sec", type=float, default=60.0)
    s.add_argument("--once", action="store_true", help="one cycle then exit (scripts / tests)")
    s = sub.add_parser("ui", help="PySide6 desktop UI; attaches to the engine's state")
    s.set_defaults(fn=cmd_ui)
    s.add_argument("--config", default="config"); s.add_argument("--refresh-ms", type=int, default=1000)

    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status); s.add_argument("--config", default="config")
    s = sub.add_parser("kill"); s.set_defaults(fn=cmd_kill); s.add_argument("--config", default="config")
    s.add_argument("--flatten", action="store_true"); s.add_argument("--reason")
    s = sub.add_parser("approve-plugins"); s.set_defaults(fn=cmd_approve)
    s.add_argument("--config", default="config"); s.add_argument("--actor", default="operator")
    s = sub.add_parser("keygen"); s.set_defaults(fn=cmd_keygen); s.add_argument("-n", type=int, default=5)
    s = sub.add_parser("drill"); s.set_defaults(fn=cmd_drill)
    s.add_argument("--config", default="config"); s.add_argument("--data"); s.add_argument("--days", type=int, default=320)
    s = sub.add_parser("watchdog"); s.set_defaults(fn=cmd_watchdog); s.add_argument("--config", default="config")
    s = sub.add_parser("verify-audit"); s.set_defaults(fn=cmd_verify_audit); s.add_argument("path")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())

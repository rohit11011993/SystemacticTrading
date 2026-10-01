# AlgoTrader: mid-frequency systematic trading for Indian markets

A Python implementation of the two design documents:

* **PRD: Algo-Trading System (Indian Markets)**: the platform (plugins, risk gateway, kill
  switches, monitoring, execution, persistence, packaging).
* **Trading Strategy Design: Indian Mid-Frequency Systematic Book**: strategies S1–S5, the
  regime engine, the shared sizing/stop/cost framework, and portfolio allocation.

> **None of the strategies has been backtested on real data.** Every threshold is a
> configurable starting proposal, as the design documents state. The bundled synthetic data
> only shows that the system runs end to end. It says nothing about edge.

## Quick start

```bash
pip install -r requirements.txt
python -m algotrader make-synthetic --out data          # demo data (replace with real data)
python -m algotrader validate-config                    # FR-7.6 configuration validator
python -m algotrader backtest --start 2020-01-01        # all five strategies, net of costs
python -m algotrader backtest --today-costs             # same, priced at today's STT/charges
python -m algotrader backtest --cost-mult 1.5           # cost-stress test (strategy doc s.11)
python -m algotrader drill                              # rehearse all four kill-switch levels
python -m algotrader run-day --date 2026-09-30          # one paper session, persistent state
python -m algotrader status                             # blotter + kill-switch states
python -m algotrader kill --flatten                     # the big red button
python -m pytest -q                                     # 66 tests (risk, OMS, kill switches...)
```

## Architecture

```
market data ─► engine ─► strategies (plugins) ─► Signals
                  │                                  │ sizing (N = r·NAV / (k·ATR·lot))
                  │                                  ▼
                  │                         RISK GATEWAY (rule chain)  ◄── kill switches, regime table
                  │                                  │ ALLOW / RESIZE / BLOCK / FLATTEN
                  ▼                                  ▼
          alignment monitor ◄── trade book ◄── order manager ──► broker adapter (paper | Kite)
                                   ▲                                  │
                                   └──────── reconciliation ◄─────────┘
```

| Package | Contents | Requirements met |
|---|---|---|
| `algotrader/core/` | Domain models, config schemas + validator, instrument registry, versioned cost table, event calendar, hash-chained audit log, SQLite (WAL) state store | R3, R4, FR-6.1, FR-7.2/7.6/7.7, FR-11.1/11.2 |
| `algotrader/strategy/` | Plugin contract (`Strategy`, `Context`), plugin facade `api.py`, sandboxed loader (import allow-list, hashes) | R3, FR-5.1–5.7, FR-15.2/15.3 |
| `algotrader/risk/` | Risk gateway with all 13 rule families, layered "strictest wins" limits, loosening cool-off, what-if, dry-run; four-level kill switches, drawdown ladder, external reset key | R4, R6, s.7, s.9 |
| `algotrader/execution/` | Broker interface, paper broker (same interface), order state machine, idempotent retries, token-bucket pacing, multi-leg groups, Kite adapter | R7, s.10, REG-1..4 |
| `algotrader/monitoring/` | Alignment indicator (7 checks, 3 states), drift statistics, reconciliation, blotter | R5, s.8, FR-11.3 |
| `algotrader/regime.py` | ER(10) × India VIX percentile regime with 2-day hysteresis and a permission table | Strategy doc s.3 |
| `algotrader/engine.py` | Daily loop shared by backtest, paper and live; save/restore after restart | FR-5.6, NFR-5/6 |
| `algotrader/backtest.py`, `research.py` | Reports; Monte Carlo drawdown, top-trade removal, deflated Sharpe, plateau grid, walk-forward windows | Strategy doc s.11 |
| `plugins/` | S1–S5 strategy plugins (loaded from outside the package/executable) | Strategy doc s.5–9 |
| `config/` | All limits, costs, instruments, allocations, kill-switch triggers, bindings, events | NFR-9 |
| `packaging/` | PyInstaller one-folder spec, Inno Setup installer, scripted build | R2, FR-13.x |

## Strategies

| ID | Plugin | Logic | Tunable parameters (test range) |
|---|---|---|---|
| S1 | `s1_adaptive_trend.py` | KAMA(10,2,30) entry, KAMA(10,5,30) trend filter, ER gate, ATR initial and trailing stops | `er_min` 0.2–0.4, `f_atr` 0.1–0.5, `k_init` 2–3.5, `k_trail` 2.5–4.5 |
| S2 | `s2_channel_breakout.py` | ATR(10)/ATR(60) compression, Donchian breakout, 3-day failed-breakout exit, limit entries | `n_entry` 15–40, `comp_max` 0.7–1.0, `k_stop` 2–3.5, `n_exit` 7–20 |
| S3 | `s3_short_term_reversal.py` | Residual (beta-adjusted) z-score reversal, ER noise gate, 100-day trend guard, whole-lot Nifty hedge within 15% tolerance, stress stand-down | `z_entry` 1–2.5, `n_hold` 3–8, `k_stop` 1.5–3, `ret_window` 2–5 |
| S4 | `s4_stat_pairs.py` | Weekly ADF + half-life pair screening with enable/disable hysteresis, z-score entries/exits/stops, lot-ratio imbalance check | `z_in` 1.5–2.5, `z_out` 0–1, `z_stop` 3–4.5, `z_window` 40–90 |
| S5 | `s5_range_iron_condor.py` | Iron condor in low-ER / mid-VIX regimes, delta-based strikes, profit/loss/delta/time exits, VIX-spike close-all, no adjustments | `short_delta` 0.1–0.25, `dte_entry` 21–45, `take_profit` 0.4–0.75, `loss_stop` 0.75–1.5 |

Parameters are validated against these ranges when a plugin is loaded. Each strategy implements
`expected_path`, which drives the green/amber/red alignment indicator on the blotter.

**Capital matters.** At ₹50 lakh, one stock-futures lot already carries more risk than S3's
0.25% or S4's 0.5% per-trade budget, so their sizing rounds to zero and they rarely trade.
This is the behaviour strategy doc s.10 predicts. They do trade at larger capital; see the tests.

## Risk gateway rule chain

`system_state → data_health → market_state → price_sanity → rate_and_duplicates →
options_structure → risk_per_trade → allocation → position_size → liquidity → exposure → margin → cost`

* Risk-reducing orders (exits, stops, flatten) run only `price_sanity` and `rate_and_duplicates` (FR-7.1).
* Each decision is written to the audit log with every rule result, the limits applied and the
  config hash (FR-7.3). Run `algotrader verify-audit <file>` to check the hash chain.

## What is not in version 1 yet

These are not implemented, and they are listed here rather than glossed over:

* **Desktop UI (PySide6).** Only the text blotter (`status`, `run-day`) exists. `monitoring.blotter`
  produces the row model the UI would render.
* **Separate gateway process over IPC.** The gateway runs in-process with the engine. Its API
  has no transport dependency, but the authenticated local IPC server (FR-12.1, FR-15.4) is not written.
* **Kite adapter has not been tested against the live API.** Field names, `market_protection`,
  AMO handling and rate limits must be checked against current Kite docs. The instrument-master
  refresh still needs the broker's trading symbol mapped into `data_symbol`.
* **Option chains are modelled** with Black-Scholes and India VIX (`data/options.py`) until a
  historical chain source is chosen (strategy doc open question 3).
* **Futures rolling** assumes continuous back-adjusted series. Live roll execution and roll-cost
  recording (FR-10.5) are not automated. The expiry cut-off rule is enforced.
* Email/messaging alerts, daily backups, clock-drift checks and the PDF/HTML reports are not
  implemented. The engine's `alerts` list and the audit log are the hooks for them.
* In **backtests only**, manual kill-switch resets are simulated 5 days after a trip
  (`backtest_auto_reset_days`), so one trip doesn't freeze the rest of a research run. Each
  simulated reset is logged as `simulated_reset`. This never applies in paper or live mode.

## Open questions from the documents (still for the principal)

Starting capital; broker segment permissions (NFO, MCX, CDS, options); the historical data source;
S3 futures vs delivery legs; which S4 pairs to screen first; the strategy stop levels and risk
shares; who holds the external reset key; whether a second broker is available as exit-only fallback.

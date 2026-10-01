"""AlgoTrader: a mid-frequency systematic trading system for Indian exchange-traded markets.

The package follows the companion documents:

* ``PRD_Algo-Trading_System_Indian_Markets.md`` (platform: plugins, risk gateway,
  kill switches, monitoring, execution, persistence), and
* ``Trading_Strategy_Design_Indian_Mid-Frequency_Systematic_Book.md`` (strategies S1-S5,
  regime engine, shared sizing / stop / cost framework, portfolio allocation).

Layout
------
core/        Domain models, configuration schemas, instruments, costs, calendar, audit, state store
data/        Market-data providers (CSV replay, synthetic generator, Kite adapter) and option chains
indicators   Efficiency ratio, KAMA, ATR, channels, z-scores, ADF test, half-life, Black-Scholes
regime       Daily regime engine with hysteresis and a strategy permission table
strategy/    Strategy plugin contract (ABC + Context) and the sandboxed plugin loader
risk/        Risk gateway (rule chain), layered limits, kill-switch manager
execution/   Broker interface, paper broker, Kite broker adapter, order manager (state machine)
monitoring/  Alignment indicator, drift monitor, reconciliation, blotter
portfolio/   Trade book (virtual books per strategy), sizing, allocation
engine       The daily orchestration loop shared by backtest, paper and live modes
backtest     Backtest runner and performance report
cli          Command-line entry point (``algotrader <sub-command>``)

None of the strategies has been backtested on real data; every threshold is a configurable
starting proposal, exactly as stated in the design documents.
"""

__version__ = "0.1.0"

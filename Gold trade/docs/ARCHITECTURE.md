# Architecture

## Data flow

```
 CSV / MT5 / synthetic M5 ─► market.py ─► frames.py (validate, dedupe, gap-check, resample)
                                              │  MN1 W1 D1 H4 H1 M15 M5 (all from one M5 source)
                                              ▼
 economic_calendar.csv ─► macro.py ─┐   analysis.py ◄── structure.py, smc.py, indicators.py
 data/news/*.json ──────► sentiment.py ─┘     │   builds MarketContext + dashboard payload
                                              ▼
                                         signals.py      BUY / SELL / NO_TRADE + evidence
                                              ▼
                                         risk.py         independent approve / reject + size
                                              ▼
                                         execution.py    analysis (record) · paper (simulate) · live (locked)
                                              ▼
                                         journal.py      append-only JSONL (+ PostgreSQL mirror)
```

`service.py` owns the runtime state and runs this pipeline. `main.py` exposes it over HTTP
and a WebSocket, and runs a **background cycle** every `AUTO_CYCLE_SECONDS`: refresh the
calendar and market data, mark paper positions bar by bar, and evaluate once per new closed
bar. The bot therefore keeps working when no dashboard is open. The React dashboard in
`frontend/` only reads from the API and sends operator controls back to it.

## Operating configuration

| Choice | Setting |
|---|---|
| Platform | MetaTrader 5 as a read-only price feed (`MT5_*`); CSV history as the alternative |
| Operation | `APP_MODE=paper`: analysis + simulated trading. Live is locked. |
| Timeframes | M15 + H1 + H4 + D1 decide. MN1, W1 and M5 are shown for context only. |
| Risk per simulated trade | `MAX_RISK_PER_TRADE_PCT=0.5` |
| Strategy | `STRATEGY_PROFILE=intraday` (default) or `scalping`; see `profiles.py` |
| Stack | Python (FastAPI) + React.js |

| Profile | Direction | Confirm | Entry | Max hold |
|---|---|---|---|---|
| intraday | D1 + H4 | H1 | M15 | 24 h |
| scalping | H4 + H1 | M15 | M15 | 4 h |

## NFP and CPI protection

`econ_calendar.py` merges three sources: the operator CSV, an optional JSON feed
(`CALENDAR_FEED_URL`, cached to `var/calendar_cache.json`), and a **built-in schedule that
needs no network**:

* **NFP**: the first Friday of the month at 08:30 New York time, with daylight saving handled.
  If that Friday falls on the 1st–3rd, the second Friday is also covered.
* **CPI**: BLS publishes no fixed rule, so every weekday from the 10th to the 15th at 08:30
  New York time is protected until a confirmed date arrives.

Once the feed or the CSV confirms a month's date, that month's estimates are dropped.
Around each NFP/CPI release the bot does three things:

1. It blocks new trades `TIER1_BLACKOUT_BEFORE_MIN` (60) before until `TIER1_BLACKOUT_AFTER_MIN`
   (60) after. The signal gate turns this into NO TRADE, and the risk gate also refuses on
   its own (`event_blackout`).
2. It closes open simulated positions `FLATTEN_BEFORE_TIER1_MIN` (15) before the release
   (`event_flatten` in the journal), because gold can gap through a stop on the number.
3. The backtest applies the same windows to every month of history.

The calendar never predicts which way the number will move gold.

## Modules

| # | Requirement | Code | Notes |
|---|---|---|---|
| 1 | Market data engine | `market.py`, `frames.py`, `sessions.py` | One M5 base, resampled with `closed=left`. Incomplete higher-TF buckets are dropped. Duplicates removed. Rejects the frame if >2% of candles are corrupt. Counts missing candles but ignores weekend gaps. Checks that the H1 close equals the close of its last M5 child. Spread is carried per bar. |
| 2 | Multi-timeframe structure | `structure.py`, `analysis.py` | Swings confirm only after 3 right-hand bars. HH/HL vs LH/LL bias, BOS/CHOCH, support/resistance clusters, supply/demand origin zones, trend/consolidation regime. MN1–H4 set direction; H1–M5 time the entry. A D1/H4/H1 disagreement sets `conflict`, which multiplies the score by 0.55. |
| 3 | Smart Money Concepts | `smc.py` | Equal highs/lows → buy/sell-side liquidity, sweeps, FVGs, order/breaker/mitigation blocks, premium/discount, displacement. Always flagged `confirmation_required`. None of these can produce a trade on its own. |
| 4 | Indicators | `indicators.py` | EMA 20/50/100/200, RSI, MACD, ATR, Bollinger, ADX/DI, Fibonacci, volume ratio. All causal (a test checks this). `indicator_agreement` is a weighted vote in [-1, 1], so no single indicator decides. |
| 5 | Macro intelligence | `macro.py` | Configurable blackout around high-impact events (default −30/+15 min). DXY and 10Y yield changes are descriptive context only. `directional_assumption` is always `None`. |
| 6 | Sentiment | `sentiment.py`, `analysis.sentiment_summary` | A transparent lexicon classifier (bullish / bearish / neutral / uncertain) weighted by source reliability and importance. Social media and unverified sources are never used as evidence. Sentiment can only lower confidence, never create a signal. |
| 7 | Signal engine | `signals.py`, `profiles.py` | Nine weighted components: higher TF, structure, liquidity, price action, indicators, volatility, macro, sentiment, execution. A logistic map converts the score to a confidence with a ceiling of 90. NO TRADE when data is missing, during a blackout, when spread is too wide, in extreme volatility, when the daily bias isn't directional, when location or confirmation fail, when R:R < min, or when confidence is below the floor. |
| 8 | Risk management | `risk.py` | Fixed-fractional sizing, rounded *down*. Limits: per-trade risk, daily loss, weekly drawdown, open positions, correlated exposure, spread, R:R, consecutive-loss pause (operator acknowledgement required), and allowed sessions. Rejects any recovery multiplier. A stop can be tightened but never widened. |
| 9 | Backtesting | `backtest.py` | Fills at the next bar's open plus half the spread and slippage. When stop and target are touched in the same bar, the stop is assumed hit. Commission is per lot. Higher-TF bias is read only from bars that had **closed** before the entry bar closed. Reports win rate, PF, return, max DD, average R:R, Sharpe, Sortino, expectancy, streaks, results by session and regime, and Monte Carlo reshuffles. `validate_out_of_sample` runs one development segment and four sequential out-of-sample segments, each starting from fresh equity. |
| 10 | Learning | `learning.py`, `service.learning_view` | Calibration table, drift detection, regime labels, and a draft logistic classifier that is never deployed automatically. `ModelRegistry` keeps an audit trail and only promotes a version after walk-forward passes. `filter_recommendations` strips any risk-limit keys. |
| 11 | Dashboard | `frontend/` | React with TradingView lightweight-charts. Live updates over WebSocket, plus toast and browser notifications when the decision changes. |
| 12 | Execution & safety | `execution.py`, `broker_mt5.py`, `state.py`, `service.py`, `journal.py` | Starts in `analysis` mode; `paper` is available. The API refuses `live` (schema) and `request_live` requires every checklist item **and** an adapter with `ready=True`. The default adapter is never ready. Emergency stop flattens paper positions and needs a typed acknowledgement to reset. Every signal, rejection, risk decision, mode change and fill is journaled. |

## Safety invariants (enforced by tests)

* `APP_MODE=live` in the environment never arms live trading by itself.
* No API route can transmit a broker order. `orders_sent_to_broker` is always 0.
* Position size never increases after a loss. Recovery multipliers are rejected.
* Stops cannot be widened.
* No new simulated trade inside an NFP/CPI window; open ones close before the release.
* A paper position is never checked against the bar it was opened on, and every later bar is checked (not only the latest).
* Each closed bar is journaled once, however often the API is polled.
* Unfinished higher-timeframe bars and the MT5 bar that is still forming are never analysed.
* The learning module cannot write risk limits.
* Indicators and backtest decisions do not change when future bars are appended.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health`, `/api/status` | Liveness, mode, data source, live checklist gaps |
| GET | `/api/analysis` | Full multi-timeframe payload |
| GET | `/api/signal` | Run and journal one decision (opens a paper position in paper mode) |
| GET | `/api/candles?timeframe=H1&limit=300` | OHLC rows |
| GET | `/api/calendar`, `/api/news` · POST `/api/news` | Macro and sentiment |
| GET | `/api/tier1` · POST `/api/calendar/refresh` | NFP & CPI monitor; force a feed fetch |
| POST | `/api/data/refresh` | Run one autonomous cycle now |
| GET | `/api/risk`, `/api/performance`, `/api/journal?kind=` | Exposure, P&L, audit log |
| POST | `/api/mode` `{mode: analysis\|paper}` | Switch mode |
| POST | `/api/live/request` | Evaluate the live checklist (rejected without a ready adapter) |
| POST | `/api/paper/mark`, `/api/paper/stop` | Mark positions; tighten-only stop updates |
| POST | `/api/emergency/shutdown`, `/api/emergency/reset` | Kill switch |
| POST | `/api/risk/acknowledge-loss-pause` | Operator acknowledgement |
| POST | `/api/backtest` · GET `/api/backtest/latest` | Backtest + walk-forward; records a model version |
| GET | `/api/learning` · POST `/api/learning/promote`, `/api/learning/recommend` | Research tools |
| WS | `/ws/stream` | Preview evaluation every `STREAM_INTERVAL_SECONDS` (never opens positions) |

Interactive docs are available at `/docs` while the server runs.

## Live execution (MT5)

`broker_mt5.py` is the only code that can place orders. It runs only when
`BROKER_ADAPTER=mt5` and live mode has been armed: the operator's dashboard confirmation,
`LIVE_TRADING_AUTHORIZED=true`, an armed emergency stop, no active emergency, and an adapter
check that passes at that moment. The adapter refuses real-money accounts unless
`ALLOW_REAL_ACCOUNT=true`, caps lots at `LIVE_MAX_LOTS`, sends broker-side SL/TP with every
order (closing the position if a stop cannot be attached), tags orders to prevent duplicates,
and never widens a stop. `service.reconcile_live()` treats MT5 as the source of truth for
open positions, realized P&L and equity. All MT5 calls share one lock with the price feed.

## Durable state

`state.py` writes `var/state.json` atomically after every change: account, anchors, loss
pause, emergency stop, mode, and open paper and live positions. Startup restores it; live
mode is never restored and must be re-armed. A corrupt file is set aside, and the bot starts
fresh with the journal intact.

## Known limitations

* The sentiment model is a lexicon, not a trained transformer. Its decisions can be audited
  phrase by phrase, but its coverage is narrow. A FinBERT-style model could replace
  `classify_news` without changing its interface.
* Backtests use a faster, simplified context (latest swing only, no sweep detection), so they
  are a screen. `scripts/replay.py` runs the exact live code path over history bar by bar
  and is the authoritative end-to-end check.
* The synthetic feed is a random walk with a sinusoidal drift. Backtests on it test the
  software, not a strategy. On that feed the strategy mostly returns NO TRADE, which is
  the expected result.
* The MT5 reader pulls M5 rates in batches. It doesn't stream ticks, so "real-time" means
  the latest closed M5 bar.
* Calendar and news ingestion are file- and API-based. No third-party provider is
  bundled; connect an authorized one.

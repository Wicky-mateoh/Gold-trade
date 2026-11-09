# Testing

```bash
cd "Gold trade" && python -m pytest
```

The suite generates all its data in-process, so it needs no network, broker or database.
It has 118 tests and runs in about 7 seconds.

| File | Covers |
|---|---|
| `test_market_data.py` | All 7 timeframes built and in sync. Duplicate removal, corrupt-candle removal, rejection of heavily corrupted data, missing-bar detection that ignores weekends, OHLC correctness after resampling, incomplete buckets dropped, session clock. |
| `test_indicators.py` | EMA/RSI/ATR/Bollinger/ADX math. **Causality**: values for past bars don't change when future bars are appended. Fibonacci levels. Agreement is bounded and no single vote maxes it. |
| `test_structure_smc.py` | Swings need right-hand confirmation. Bullish/bearish bias, BOS/CHOCH, conflict detection, SMC flagged as context only, equal-highs sweep. |
| `test_macro_sentiment.py` | Blackout window edges. Medium-impact events don't block. News labels. Social media never used as evidence. Sentiment lookback and weighting. |
| `test_signals.py` | Full BUY/SELL payload with all required fields. Every NO TRADE condition (parametrized). Conflict and opposing sentiment lower confidence. SMC alone isn't a signal. |
| `test_risk_execution.py` | Size capped and rounded down. Spread, R:R, stop side, session and over-risk rejections. Recovery multiplier rejected. Daily and weekly limits. Loss-streak pause with size never growing. Max open positions. Stops can't widen. Learning can't edit risk keys. Live locked without every checklist item and a ready adapter. Paper fills. |
| `test_backtest_learning.py` | Trades follow the trend, regime labels are real. **No look-ahead**: truncating the history leaves earlier trades unchanged. A random walk doesn't pass the gate. Costs reduce P&L. Metric math. Monte Carlo ordering. Walk-forward segment roles. Calibration/drift/regime. The classifier stays in draft. Model promotion requires validation and persists. |
| `test_nfp_cpi.py` | NFP first-Friday rule (with DST and the early-Friday case), CPI candidate days, feed parsing, confirmed dates replacing estimates, cache kept when the feed fails, tier-1 window edges (−60/+60), ordinary events keep the −30/+15 window, countdown and flatten flags. |
| `test_live_path.py` | The live service path end to end. One open position max. Same bar journaled once. The entry bar isn't used to mark the position. Every new bar is marked. Equity matches the journal. Flatten before NFP. No trade inside the window (signal gate and risk gate separately). Max-hold exit. Slippage protection. TP2 beyond TP1 and R:R surviving rounding. Profile roles. Unfinished higher-TF bars dropped. The daily break doesn't delete H4 bars. The backtest skips NFP/CPI windows. CSV refresh detects new bars. |
| `test_mt5.py` | MT5 reader against a stand-in for the Windows-only package: broker server time shifted to UTC, spread from 2- and 3-digit point sizes, offset saved and reused when the market is closed, unknown offset flagged, wrong symbol lists the broker's gold symbols, hidden symbol added to Market Watch, login errors explained, contract-size mismatch warned, `DATA_SOURCE=mt5` overriding a CSV. |
| `test_config.py` | `.env.example` loads as shipped and lists every setting. |
| `test_api.py` | Status, analysis, signals journaled, candles, live can't be enabled through the API, emergency stop/reset cycle, backtest records a model version, recommendations stripped of risk keys, news ingest, journal persistence. |

## Strategy validation workflow

1. **Load real history** (`data/XAUUSD_M5.csv` or MT5). Use at least two years so the
   four out-of-sample segments each cover several regimes.
2. **Run a backtest** from the dashboard or with `POST /api/backtest {"timeframe": "H1"}`.
   Costs come from the per-bar spread column, plus commission and slippage (configurable).
3. **Read the walk-forward table.** It passes only with ≥30 out-of-sample trades, at least
   75% of out-of-sample segments net positive, and a pooled profit factor ≥ 1.1. The
   holdout deployment gate also requires a holdout PF ≥ 1.05 and a max drawdown ≤ 15%.
4. **Check calibration.** Any confidence bucket flagged `overconfident` (the predicted rate
   is more than 15 points above the realized one) means the confidence mapping needs
   recalibration before you rely on it.
5. **Paper trade** for a meaningful sample (`POST /api/mode {"mode":"paper"}`). Paper fills
   apply half the spread on entry and slippage on exit. Compare paper results with the
   backtest's out-of-sample expectancy.
6. Only then is a **live review** reasonable (see DEPLOYMENT.md). Passing these gates is
   evidence, not a guarantee.

## End-to-end replay (live code path)

```bash
cd "Gold trade" && python scripts/replay.py --csv data/XAUUSD_M5.csv --start-days 120 --step-bars 12
```

This feeds history into the running service one step at a time, as a broker would, and
reports decisions, risk rejections, paper trades, exit reasons, NFP/CPI-blocked steps, and
whether equity matches the journal. `--demo trend` and `--demo random` use generated data;
`--profile scalping` switches strategy. Expect about 0.25 s per step.

## Frontend

```bash
cd "Gold trade/frontend" && npm ci && npm run build
```

CI (`.github/workflows/ci.yml`) runs the Python suite and the dashboard build on every push.

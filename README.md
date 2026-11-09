# Gold Intelligence Trading System (GITS)

Analysis and paper-trading software for **XAU/USD**. It combines multi-timeframe market
structure, Smart Money Concepts, technical indicators, a macro calendar, and news sentiment
into a signal gate that returns **BUY**, **SELL**, or **NO TRADE**. An independent risk engine
then approves or rejects each proposal.

**Configured for:** MetaTrader 5 data · analysis + simulated trading · M15/H1/H4/D1 ·
0.5% risk per simulated trade · intraday profile (scalping available) · Python + React ·
automatic NFP and CPI protection (no new trades around the release; open trades close
before it).

> **No guarantee of profit.** Every signal carries its evidence, a calibrated confidence score,
> invalidation conditions, and risk parameters. Live order routing is **locked by default**.
> It needs explicit authorization, validated broker connectivity, account risk limits, an armed
> emergency shutdown, *and* a reviewed broker adapter. None of these is shipped enabled.

All code lives in [`Gold trade/`](Gold%20trade/).

| Doc | Contents |
| --- | --- |
| [Architecture](Gold%20trade/docs/ARCHITECTURE.md) | Modules 1–12, data flow, safety design |
| [Testing](Gold%20trade/docs/TESTING.md) | Test suite, what each test proves, validation workflow |
| [Deployment](Gold%20trade/docs/DEPLOYMENT.md) | Docker, PostgreSQL/Redis, MT5, going from analysis → paper → live review |

## Quick start (local)

Requires Python 3.12+ and Node 22+.

```bash
cd "Gold trade"
```

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

```bash
PYTHONPATH=backend uvicorn app.main:app --reload --port 8000
```

In a second terminal, start the dashboard dev server (it proxies `/api` and `/ws` to :8000):

```bash
cd "Gold trade/frontend" && npm install && npm run dev
```

Open http://localhost:5173. With no data configured, the system runs on a **labelled synthetic
price path** with example calendar and news rows. This is for exercising the software only.

## Quick start (Docker)

```bash
cd "Gold trade" && docker compose up --build
```

Open http://localhost:8000. The image builds the dashboard and serves it from FastAPI,
alongside PostgreSQL (journal mirror) and Redis (snapshot cache).

## Run the tests

```bash
cd "Gold trade" && python -m pytest
```

## Using real data

Put an M5 history file at `Gold trade/data/XAUUSD_M5.csv` with the columns
`time,open,high,low,close,volume[,spread]`. Every other timeframe is resampled from it. Or
configure the `MT5_*` variables on a Windows host running the MetaTrader 5 terminal
(read-only rates). Copy `data/economic_calendar.example.csv` to `data/economic_calendar.csv` and
fill it from official schedules. Drop news JSON files into `data/news/`.

# Deployment

## Docker Compose (recommended)

```bash
cd "Gold trade" && cp .env.example .env && docker compose up --build -d
```

| Service | Port | Purpose |
|---|---|---|
| `api` | 8000 | FastAPI + built dashboard. The journal is stored in the `gits_var` volume. |
| `postgres` | 5432 | Journal mirror. `db/schema.sql` is applied on first start. |
| `redis` | 6379 | Cache for the latest analysis snapshot |

The compose file sets `APP_MODE=analysis` and `LIVE_TRADING_AUTHORIZED=false`, which override
`.env.example`. Change these deliberately, not by accident.

PostgreSQL and Redis are optional. When `DATABASE_URL` / `REDIS_URL` are empty or unreachable,
the journal falls back to `var/journal.jsonl` and the cache falls back to memory. `/api/status`
reports which backends are active.

**Before exposing the API beyond localhost**, put it behind an authenticating reverse proxy.
The control routes (mode, emergency stop) have no authentication of their own. Restrict
`CORS_ORIGINS` as well.

## Bare metal

```bash
cd "Gold trade" && pip install -r requirements.txt
```

```bash
cd "Gold trade/frontend" && npm ci && npm run build
```

```bash
cd "Gold trade" && PYTHONPATH=backend GITS_ROOT=. uvicorn app.main:app --host 0.0.0.0 --port 8000
```

FastAPI serves the built dashboard at `/` whenever `frontend/dist` exists.

## Market data

Sources are tried in this order:

1. `data/XAUUSD_M5.csv` with columns `time,open,high,low,close,volume[,spread]`, times in UTC.
2. **MetaTrader 5** (read-only). Needs Windows with the MT5 terminal installed,
   `pip install MetaTrader5`, and `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `MT5_SYMBOL` set.
   The reader requests M5 rates and never sends orders. MT5 reports spread in points;
   `market.py` multiplies it by 0.01, so confirm that matches your broker's contract.
3. **Synthetic**, used when neither is available. The dashboard shows a banner, and the
   example calendar and news rows are loaded.

Data loads once at startup. Restart the service, or add a scheduler that calls
`MarketDataEngine.load_base`, to pull new bars.

## Contract settings

Set `CONTRACT_OUNCES`, `MIN_LOT` and `LOT_STEP` from your broker's XAUUSD specification.
Position size depends on them directly.

## Path to live: a review, not a switch

Live execution needs **all** of the following. The software enforces each one:

1. `LIVE_TRADING_AUTHORIZED=true`. This is explicit operator authorization.
2. `BROKER_CONNECTED=true`, backed by a real connectivity check in the adapter.
3. Account-specific risk limits set in `.env` and reviewed.
4. `EMERGENCY_SHUTDOWN_ARMED=true`, with the kill switch tested in paper mode.
5. A `BrokerAdapter` subclass with `ready = True` and a reviewed `submit()`. **None ships with
   this repository.** It must enforce server-side stops and respect `propose_stop_update`.
6. A walk-forward **pass** on real data, plus a paper-trading sample that is consistent with it.

Even then, `request_live` only arms the mode. Order transmission goes through the adapter
you supply, and that adapter is your responsibility to test.

## Operations

* **Emergency stop**: use the dashboard button or `POST /api/emergency/shutdown {"reason": "..."}`.
  It flattens paper positions and disables live. To reset, the operator must type
  "I have reviewed the shutdown cause".
* **Loss-streak pause**: after `MAX_CONSECUTIVE_LOSSES` losses, new risk stops until an operator
  acknowledges it. Size is not increased afterwards.
* **Audit**: `GET /api/journal`, `var/journal.jsonl`, or the `journal` table in PostgreSQL.
* **Health**: `GET /api/health`. The Docker image includes a HEALTHCHECK.

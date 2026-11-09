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
2. **MetaTrader 5** (read-only), see *Connecting your MetaTrader 5 account* below. Bar times
   are converted from broker server time to UTC, and the spread from points using the
   symbol's own point size. `DATA_SOURCE=mt5` forces MT5 even if a CSV exists.
3. **Synthetic**, used when neither is available. The dashboard shows a banner, and the
   example calendar and news rows are loaded.

The background cycle (`AUTO_CYCLE_SECONDS`, default 60) reloads the CSV or pulls MT5 rates,
checks open simulated positions against every new bar, and evaluates once per new closed
bar. A failed pull keeps the last good data and is journaled.

## Connecting your MetaTrader 5 account

MT5's Python connection only works on **Windows**, on the same PC as the MT5 terminal.
Docker containers run Linux, so for MT5 use the bare-metal setup on that Windows PC.

1. **Install and log in to MT5.** Install your broker's MetaTrader 5 terminal, log in once
   by hand (File > Login to Trade Account), and leave it installed. A demo account is fine.
2. **Load enough history.** In MT5 open Tools > Options > Charts and set *Max bars in chart*
   to at least 100000, then open an XAUUSD **M5** chart once so the terminal downloads it.
3. **Install Python 3.12+ and Node 22+** on the PC, then from the `Gold trade` folder run:

   ```bat
   pip install -r requirements.txt
   ```

   On Windows this also installs the `MetaTrader5` package.
4. **Create `.env`** from `.env.example` and fill in:

   ```ini
   DATA_SOURCE=mt5
   MT5_LOGIN=12345678            # your account number
   MT5_PASSWORD=your_password    # the investor (read-only) password is enough
   MT5_SERVER=YourBroker-Demo    # exactly as shown in MT5's login window
   MT5_SYMBOL=XAUUSD             # your broker's exact gold symbol
   ```

   `.env` is git-ignored. Never commit it.
5. **Check the connection** (read-only, sends no orders):

   ```bat
   python scripts\check_mt5.py
   ```

   It confirms the login, lists your broker's gold symbols if `MT5_SYMBOL` is wrong, detects
   the broker's server-time offset from UTC, compares contract size and lot rules with
   `CONTRACT_OUNCES` / `MIN_LOT` / `LOT_STEP`, and prints the latest bars in UTC. Fix
   anything marked `[WARN]` or `[FAIL]` and run it again until it says **ready**. Run it
   while the market is open so the time offset can be detected and saved.
6. **Start the bot:**

   ```bat
   cd frontend && npm ci && npm run build && cd ..
   set PYTHONPATH=backend
   uvicorn app.main:app --port 8000
   ```

   Open http://localhost:8000. The header should say **metatrader5 data**, and
   `/api/status` shows the broker, the bar count, and the UTC offset in use.

What the bot does with MT5: every `AUTO_CYCLE_SECONDS` (default 60) it reads the latest
**closed** M5 bars, converts broker server time to UTC, and runs analysis and simulated
trading. It never sends orders to your account.

## Running unattended on Windows

```bat
powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1
```

This registers a Windows scheduled task that starts `scripts\windows\run_gits.bat` at your
logon. The batch file restarts the server 15 seconds after any exit. MT5 needs a logged-in
desktop, so on a PC or VPS that must survive reboots, also turn on automatic sign-in for that
Windows account. Remove the task with `scripts\windows\uninstall_autostart.ps1`.

**State survives restarts.** `var/state.json` holds the account balance, the day and week
anchors, the loss-streak pause, the emergency stop, the mode, open simulated positions and
tracked live positions. It is rewritten atomically after every change. On restart the bot
journals "State restored after restart". **Live mode is never restored automatically**: it
comes back as paper and must be re-armed from the dashboard. Live positions stay tracked, so
broker-side stop and target fills are still recorded.

Logs: `var/logs/gits.log` (rotating, 5 x 5 MB), plus `server.out` and `supervisor.log`
from the batch runner.

## Stage 3: real orders on an MT5 demo account

Only after the paper-trading stage. Every one of these is required:

1. Log in to MT5 with the account's **master (trading) password**; the investor password
   cannot trade. Use a **demo** account.
2. Click **Algo Trading** in the MT5 toolbar so it turns green.
3. In `.env`:

   ```ini
   BROKER_ADAPTER=mt5
   LIVE_TRADING_AUTHORIZED=true
   LIVE_MAX_LOTS=0.01
   ALLOW_REAL_ACCOUNT=false
   ```

4. Restart the bot. The header shows **MT5 demo ready**. If it doesn't, the banner lists why
   (Algo Trading off, investor password, real account, wrong symbol...).
5. Press **live** on the dashboard and type the confirmation phrase.

What the connector guarantees:

* **Demo only:** a real-money account is refused unless `ALLOW_REAL_ACCOUNT=true`, and this
  is checked again before every order.
* **Broker-side protection:** every order carries its stop-loss and take-profit. If a fill
  comes back without its stop, the stop is re-sent; if that fails, the position is closed.
* **Lot cap:** `LIVE_MAX_LOTS` applies after the risk sizing.
* **No duplicates:** each order is tagged (`magic` + `gits:<id>`) and checked against open
  positions and recent deal history before sending.
* **Slippage:** the fill must sit within the entry zone ± `MAX_SLIPPAGE`, and MT5's
  `deviation` enforces the same limit on the server.
* **Reconciliation:** each cycle reads MT5 positions. Broker stop, target and stop-out fills
  are journaled with the real P&L from deal history, equity is taken from the broker, and
  any position carrying the bot's magic number is adopted.
* **Closes:** the emergency stop, the pre-NFP/CPI flatten and the max hold close live
  positions on MT5.

Run the demo stage for several weeks. Check the journal for any `rejected`, `refused`,
`close_failed` or `closed_unprotected` events, and compare live fills with the paper
results. Only then consider `ALLOW_REAL_ACCOUNT=true`, at the minimum lot size.

## NFP & CPI calendar

The built-in schedule protects NFP and CPI with no setup. For confirmed dates plus
forecast/previous/actual values, either:

* set `CALENDAR_FEED_URL` to a JSON feed of ForexFactory-style rows (`title, country, date,
  impact, forecast, previous[, actual]`). It's fetched every `CALENDAR_REFRESH_MINUTES`
  and cached in `var/calendar_cache.json`; or
* fill `data/economic_calendar.csv` from bls.gov / federalreserve.gov schedules.

Check the source's terms of use before automated fetching. The dashboard's NFP & CPI panel
shows whether each next release is *confirmed* or *estimated*.

## Contract settings

Set `CONTRACT_OUNCES`, `MIN_LOT` and `LOT_STEP` from your broker's XAUUSD specification.
Position size depends on them directly.

## Path to live: a review, not a switch

Live execution needs **all** of the following. The software enforces each one
(see *Stage 3* above for the MT5 demo procedure):

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

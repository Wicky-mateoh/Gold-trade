"""HTTP API for the Gold Intelligence Trading System.

All routes are read-only analysis or paper-trading controls. No route can
send a live order: live mode needs the operator checklist and a broker
adapter that reports ready, and the default adapter never does.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __version__
from app.config import load_settings
from app.execution import LiveChecklist
from app.market import TIMEFRAMES
from app.sentiment import NewsItem
from app.service import TradingService

ROOT = Path(os.environ.get("GITS_ROOT", Path(__file__).resolve().parents[2]))
FRONTEND_DIST = ROOT / "frontend" / "dist"


class ModeRequest(BaseModel):
    mode: str = Field(pattern="^(analysis|paper)$")


class LiveRequest(BaseModel):
    explicit_authorization: bool = False
    broker_connected: bool = False
    account_risk_limits_set: bool = False
    emergency_shutdown_armed: bool = False


class ShutdownRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class ResetRequest(BaseModel):
    operator: str = Field(min_length=1, max_length=120)
    acknowledgement: str


class OperatorRequest(BaseModel):
    operator: str = Field(min_length=1, max_length=120)


class StopUpdate(BaseModel):
    position_id: str
    new_stop: float


class BacktestRequest(BaseModel):
    timeframe: str = Field(default="H1", pattern="^(M15|H1|H4|D1)$")
    commission_per_lot: float = Field(default=3.5, ge=0)
    slippage: float = Field(default=0.05, ge=0)


class NewsRequest(BaseModel):
    published_at: datetime
    source: str
    headline: str
    body: str = ""
    importance: str = Field(default="medium", pattern="^(low|medium|high)$")


class PromoteRequest(BaseModel):
    version: str


def create_app(service: TradingService | None = None) -> FastAPI:
    holder: dict[str, TradingService] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        holder["svc"] = service or TradingService(load_settings(), ROOT)
        yield

    app = FastAPI(title="Gold Intelligence Trading System", version=__version__, lifespan=lifespan)
    origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    def svc() -> TradingService:
        if "svc" not in holder:
            holder["svc"] = service or TradingService(load_settings(), ROOT)
        return holder["svc"]

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "time": datetime.now(timezone.utc).isoformat()}

    @app.get("/api/status")
    def status():
        return svc().status()

    @app.get("/api/analysis")
    def analysis():
        return svc().analysis()

    @app.get("/api/signal")
    def signal():
        return svc().evaluate()

    @app.get("/api/candles")
    def candles(timeframe: str = Query("H1"), limit: int = Query(300, ge=10, le=5000)):
        if timeframe not in TIMEFRAMES:
            raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAMES)}")
        return svc().candles(timeframe, limit)

    @app.get("/api/calendar")
    def calendar():
        return svc().analysis()["macro"]

    @app.get("/api/news")
    def news():
        return svc().analysis()["sentiment"]

    @app.post("/api/news")
    def add_news(body: NewsRequest):
        return svc().add_news(NewsItem(body.published_at, body.source, body.headline, body.body, body.importance))

    @app.get("/api/risk")
    def risk():
        return svc().risk_view()

    @app.get("/api/performance")
    def performance():
        return svc().performance()

    @app.get("/api/journal")
    def journal(kind: str | None = None, limit: int = Query(100, ge=1, le=1000)):
        return svc().journal.query(kind, limit)

    @app.post("/api/mode")
    def mode(body: ModeRequest):
        return svc().set_mode(body.mode)

    @app.post("/api/live/request")
    def live(body: LiveRequest):
        return svc().request_live(LiveChecklist(**body.model_dump()))

    @app.post("/api/paper/mark")
    def mark():
        return {"closed": svc().mark_paper()}

    @app.post("/api/paper/stop")
    def stop(body: StopUpdate):
        return svc().tighten_stop(body.position_id, body.new_stop)

    @app.post("/api/emergency/shutdown")
    def shutdown(body: ShutdownRequest):
        return svc().emergency_shutdown(body.reason)

    @app.post("/api/emergency/reset")
    def reset(body: ResetRequest):
        return svc().reset_emergency(body.operator, body.acknowledgement)

    @app.post("/api/risk/acknowledge-loss-pause")
    def ack(body: OperatorRequest):
        return svc().acknowledge_loss_pause(body.operator)

    @app.post("/api/backtest")
    def backtest(body: BacktestRequest):
        return svc().backtest(body.timeframe, body.commission_per_lot, body.slippage)

    @app.get("/api/backtest/latest")
    def backtest_latest():
        return svc().last_backtest or {"note": "No backtest has run in this process."}

    @app.get("/api/learning")
    def learning():
        return svc().learning_view()

    @app.post("/api/learning/promote")
    def promote(body: PromoteRequest):
        return svc().promote_model(body.version)

    @app.post("/api/learning/recommend")
    def recommend(body: dict):
        return svc().recommend(body)

    @app.websocket("/ws/stream")
    async def stream(ws: WebSocket):
        """Push an evaluation every N seconds. Clients show notifications on decision changes."""
        await ws.accept()
        interval = float(os.environ.get("STREAM_INTERVAL_SECONDS", "15"))
        try:
            while True:
                result = await asyncio.to_thread(svc().evaluate, None, False)
                await ws.send_text(json.dumps({"type": "evaluation", "data": result}))
                await asyncio.sleep(interval)
        except WebSocketDisconnect:
            return

    if FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/")
        def index():
            return FileResponse(FRONTEND_DIST / "index.html")

    return app


app = create_app()

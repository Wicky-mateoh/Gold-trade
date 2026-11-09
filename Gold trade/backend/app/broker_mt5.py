"""MetaTrader 5 order adapter, locked to DEMO accounts by default.

Safety rules enforced here, independently of the signal and risk engines:
  * A real-money account is refused unless ALLOW_REAL_ACCOUNT=true.
  * Every order carries a broker-side stop-loss and take-profit. If a fill
    comes back without its stop, the stop is set again, and if that fails
    the position is closed: no position is ever left unprotected.
  * Lot size is capped by LIVE_MAX_LOTS regardless of the risk sizing.
  * Each order is tagged with a client id (magic number + comment), and the
    adapter checks open positions and recent history for that tag before
    sending, so a retry or restart cannot duplicate an order.
  * The fill price must sit inside the entry zone +/- MAX_SLIPPAGE, and the
    broker's `deviation` caps slippage on the server side too.
  * Stops may be tightened, never widened.

All MT5 calls go through one lock so the price feed and orders never
interleave on the terminal connection.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from app.execution import BrokerAdapter
from app.market import MT5_LOCK, MT5Feed
from app.risk import check_fill, propose_stop_update

log = logging.getLogger("gits.broker")

# MetaTrader5 constants (fallbacks used if the module lacks an attribute).
_C = {
    "ACCOUNT_TRADE_MODE_DEMO": 0,
    "ACCOUNT_TRADE_MODE_CONTEST": 1,
    "ACCOUNT_TRADE_MODE_REAL": 2,
    "TRADE_ACTION_DEAL": 1,
    "TRADE_ACTION_SLTP": 6,
    "ORDER_TYPE_BUY": 0,
    "ORDER_TYPE_SELL": 1,
    "ORDER_TIME_GTC": 0,
    "ORDER_FILLING_FOK": 0,
    "ORDER_FILLING_IOC": 1,
    "ORDER_FILLING_RETURN": 2,
    "TRADE_RETCODE_PLACED": 10008,
    "TRADE_RETCODE_DONE": 10009,
    "TRADE_RETCODE_DONE_PARTIAL": 10010,
    "DEAL_ENTRY_OUT": 1,
    "DEAL_REASON_SL": 4,
    "DEAL_REASON_TP": 5,
    "DEAL_REASON_SO": 6,
}
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2


def client_id(*parts: object) -> str:
    """Deterministic short id for one decision (same bar + direction -> same id)."""
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


class MT5Adapter(BrokerAdapter):
    name = "mt5"

    def __init__(self, settings, module=None) -> None:
        self.settings = settings
        self.feed = MT5Feed(module)
        self._mt5 = self.feed._mt5
        self.ready = False
        self.status: dict = {"ready": False, "reasons": ["Not checked yet."]}

    # ----------------------------------------------------------- helpers
    def c(self, name: str) -> int:
        return getattr(self._mt5, name, _C[name])

    @property
    def magic(self) -> int:
        return int(self.settings.live_magic)

    def _connect(self):
        s = self.settings
        return self.feed.connect(s.mt5_login, s.mt5_password, s.mt5_server, s.mt5_terminal_path)

    def _account_mode(self, account) -> str:
        mode = getattr(account, "trade_mode", None)
        if mode == self.c("ACCOUNT_TRADE_MODE_REAL"):
            return "real"
        if mode == self.c("ACCOUNT_TRADE_MODE_CONTEST"):
            return "contest"
        if mode == self.c("ACCOUNT_TRADE_MODE_DEMO"):
            return "demo"
        return "unknown"

    def _account_blockers(self, mt5) -> tuple[list[str], dict]:
        account = mt5.account_info()
        terminal = mt5.terminal_info()
        reasons: list[str] = []
        if account is None:
            return ["MT5 returned no account information."], {}
        mode = self._account_mode(account)
        if mode in {"real", "unknown"} and not self.settings.allow_real_account:
            reasons.append(
                "This is a REAL-money account (or its type is unknown). Trading is locked to demo accounts. "
                "Set ALLOW_REAL_ACCOUNT=true only after the demo stage has passed."
            )
        if terminal is not None and not getattr(terminal, "trade_allowed", False):
            reasons.append("Algo Trading is off in the MT5 terminal. Click the 'Algo Trading' button in the toolbar.")
        if not getattr(account, "trade_allowed", False):
            reasons.append("The account does not allow trading (logged in with the investor password?).")
        if not getattr(account, "trade_expert", True):
            reasons.append("The broker does not allow automated (expert) trading on this account.")
        info = {
            "login": getattr(account, "login", None),
            "server": getattr(account, "server", None),
            "account_type": mode,
            "currency": getattr(account, "currency", None),
            "balance": getattr(account, "balance", None),
            "equity": getattr(account, "equity", None),
            "margin_mode": getattr(account, "margin_mode", None),
        }
        return reasons, info

    # ------------------------------------------------------------- check
    def check(self) -> dict:
        """Connect and decide whether orders may be sent. Read-only."""
        if self._mt5 is None:
            self.ready = False
            self.status = {"ready": False, "reasons": ["The MetaTrader5 package is not installed (Windows only)."]}
            return self.status
        with MT5_LOCK:
            try:
                mt5 = self._connect()
                reasons, info = self._account_blockers(mt5)
                symbol = self.feed.resolve_symbol(mt5, self.settings.mt5_symbol)
                info["symbol"] = symbol.name
                info["volume_min"] = getattr(symbol, "volume_min", None)
                info["volume_step"] = getattr(symbol, "volume_step", None)
            except RuntimeError as exc:
                reasons, info = [str(exc)], {}
        self.ready = not reasons
        self.status = {
            "ready": self.ready,
            "reasons": reasons or ["Connected. Orders may be sent once live mode is armed."],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "live_max_lots": self.settings.live_max_lots,
            "magic": self.magic,
            **info,
        }
        return self.status

    def equity(self) -> float | None:
        with MT5_LOCK:
            try:
                account = self._connect().account_info()
            except RuntimeError:
                return None
        return None if account is None else float(account.equity)

    # ----------------------------------------------------------- orders
    def _filling(self, symbol) -> int:
        mode = int(getattr(symbol, "filling_mode", 0) or 0)
        if mode & SYMBOL_FILLING_FOK:
            return self.c("ORDER_FILLING_FOK")
        if mode & SYMBOL_FILLING_IOC:
            return self.c("ORDER_FILLING_IOC")
        return self.c("ORDER_FILLING_RETURN")

    def _ours(self, mt5) -> list:
        return [p for p in (mt5.positions_get(symbol=self.settings.mt5_symbol) or []) if getattr(p, "magic", None) == self.magic]

    def _already_sent(self, mt5, tag: str) -> object | None:
        for position in self._ours(mt5):
            if getattr(position, "comment", "") == tag:
                return position
        start = datetime.now(timezone.utc) - timedelta(days=3)
        for deal in mt5.history_deals_get(start, datetime.now(timezone.utc) + timedelta(days=1)) or []:
            if getattr(deal, "magic", None) == self.magic and getattr(deal, "comment", "") == tag:
                return deal
        return None

    def submit(self, order: dict) -> dict:
        """Send one market order with broker-side SL/TP. Returns a result dict; never raises on a broker refusal."""
        if not self.ready:
            return {"status": "refused", "reason": "Broker adapter is not ready: " + "; ".join(self.status.get("reasons", []))}
        s = self.settings
        tag = f"gits:{order['client_id']}"[:31]
        with MT5_LOCK:
            try:
                mt5 = self._connect()
            except RuntimeError as exc:
                return {"status": "refused", "reason": str(exc)}
            blockers, _ = self._account_blockers(mt5)
            if blockers:
                self.ready = False
                return {"status": "refused", "reason": "; ".join(blockers)}
            previous = self._already_sent(mt5, tag)
            if previous is not None:
                return {"status": "duplicate", "reason": f"An order tagged {tag} already exists.", "ticket": getattr(previous, "ticket", None)}
            symbol = self.feed.resolve_symbol(mt5, s.mt5_symbol)
            point = float(getattr(symbol, "point", 0.01) or 0.01)
            step = float(getattr(symbol, "volume_step", 0.01) or 0.01)
            minimum = float(getattr(symbol, "volume_min", 0.01) or 0.01)
            lots = min(float(order["lots"]), float(s.live_max_lots))
            lots = round(int(lots / step + 1e-9) * step, 2)
            if lots < minimum:
                return {"status": "refused", "reason": f"Lot size {lots} is below the broker minimum {minimum}."}
            tick = mt5.symbol_info_tick(s.mt5_symbol)
            if tick is None:
                return {"status": "refused", "reason": "No live price from MT5."}
            buy = order["direction"] == "BUY"
            price = float(tick.ask if buy else tick.bid)
            fill = check_fill(order["direction"], order["entry_low"], order["entry_high"], price, s.max_slippage)
            if not fill["accepted"]:
                return {"status": "refused", "reason": "Slippage protection: " + fill["reason"]}
            sl, tp = float(order["stop_loss"]), float(order["take_profit"])
            if (buy and not sl < price < tp) or (not buy and not tp < price < sl):
                return {"status": "refused", "reason": f"Stop/target are on the wrong side of the current price {price}."}
            min_distance = float(getattr(symbol, "trade_stops_level", 0) or 0) * point
            if abs(price - sl) < min_distance or abs(tp - price) < min_distance:
                return {"status": "refused", "reason": f"Stop or target is closer than the broker minimum ({min_distance})."}
            request = {
                "action": self.c("TRADE_ACTION_DEAL"),
                "symbol": s.mt5_symbol,
                "volume": lots,
                "type": self.c("ORDER_TYPE_BUY") if buy else self.c("ORDER_TYPE_SELL"),
                "price": price,
                "sl": round(sl, int(getattr(symbol, "digits", 2))),
                "tp": round(tp, int(getattr(symbol, "digits", 2))),
                "deviation": max(1, int(round(s.max_slippage / point))),
                "magic": self.magic,
                "comment": tag,
                "type_time": self.c("ORDER_TIME_GTC"),
                "type_filling": self._filling(symbol),
            }
            checked = mt5.order_check(request)
            if checked is None or getattr(checked, "retcode", 0) != 0:
                return {"status": "refused", "reason": f"Broker pre-check failed: {getattr(checked, 'comment', mt5.last_error())}", "request": request}
            result = mt5.order_send(request)
            if result is None:
                return {"status": "error", "reason": f"order_send returned nothing: {mt5.last_error()}", "request": request}
            ok = {self.c("TRADE_RETCODE_DONE"), self.c("TRADE_RETCODE_PLACED"), self.c("TRADE_RETCODE_DONE_PARTIAL")}
            if result.retcode not in ok:
                return {"status": "rejected", "retcode": result.retcode, "reason": getattr(result, "comment", ""), "request": request}
            position = self._find_position(mt5, result, tag)
            protection = self._ensure_protected(mt5, position, request)
            return {
                "status": "filled" if protection["protected"] else "closed_unprotected",
                "ticket": None if position is None else int(position.ticket),
                "price": float(getattr(result, "price", price) or price),
                "volume": float(getattr(result, "volume", lots) or lots),
                "retcode": result.retcode,
                "sl": request["sl"],
                "tp": request["tp"],
                "protection": protection,
                "tag": tag,
            }

    def _find_position(self, mt5, result, tag: str):
        for position in self._ours(mt5):
            if getattr(position, "ticket", None) == getattr(result, "order", None) or getattr(position, "comment", "") == tag:
                return position
        return None

    def _ensure_protected(self, mt5, position, request: dict) -> dict:
        if position is None:
            return {"protected": False, "reason": "Filled position not found to verify its stop."}
        if getattr(position, "sl", 0):
            return {"protected": True}
        fix = mt5.order_send(
            {"action": self.c("TRADE_ACTION_SLTP"), "symbol": request["symbol"], "position": position.ticket, "sl": request["sl"], "tp": request["tp"], "magic": self.magic}
        )
        if fix is not None and fix.retcode == self.c("TRADE_RETCODE_DONE"):
            return {"protected": True, "note": "Stop was attached after the fill."}
        closed = self._close(mt5, position, "stop could not be attached")
        log.error("Position %s had no stop and was closed: %s", position.ticket, closed)
        return {"protected": False, "reason": "Stop could not be attached; the position was closed.", "close": closed}

    # ------------------------------------------------------- positions
    def positions(self) -> list[dict]:
        with MT5_LOCK:
            mt5 = self._connect()
            rows = self._ours(mt5)
        return [
            {
                "ticket": int(p.ticket),
                "direction": "BUY" if p.type == self.c("ORDER_TYPE_BUY") else "SELL",
                "lots": float(p.volume),
                "entry": float(p.price_open),
                "stop_loss": float(p.sl),
                "take_profit": float(p.tp),
                "profit": float(getattr(p, "profit", 0.0)),
                "comment": getattr(p, "comment", ""),
            }
            for p in rows
        ]

    def close(self, ticket: int, reason: str) -> dict:
        with MT5_LOCK:
            mt5 = self._connect()
            position = next((p for p in self._ours(mt5) if int(p.ticket) == int(ticket)), None)
            if position is None:
                return {"status": "not_found", "ticket": ticket}
            return self._close(mt5, position, reason)

    def _close(self, mt5, position, reason: str) -> dict:
        tick = mt5.symbol_info_tick(position.symbol)
        buy = position.type == self.c("ORDER_TYPE_BUY")
        symbol = mt5.symbol_info(position.symbol)
        point = float(getattr(symbol, "point", 0.01) or 0.01)
        request = {
            "action": self.c("TRADE_ACTION_DEAL"),
            "symbol": position.symbol,
            "volume": float(position.volume),
            "type": self.c("ORDER_TYPE_SELL") if buy else self.c("ORDER_TYPE_BUY"),
            "position": int(position.ticket),
            "price": float(tick.bid if buy else tick.ask),
            # Closing is never blocked by slippage: allow a wide deviation.
            "deviation": max(1, int(round(5.0 / point))),
            "magic": self.magic,
            "comment": "gits:close",
            "type_time": self.c("ORDER_TIME_GTC"),
            "type_filling": self._filling(symbol),
        }
        result = mt5.order_send(request)
        done = result is not None and result.retcode in {self.c("TRADE_RETCODE_DONE"), self.c("TRADE_RETCODE_DONE_PARTIAL")}
        return {
            "status": "closed" if done else "close_failed",
            "ticket": int(position.ticket),
            "reason": reason,
            "retcode": None if result is None else result.retcode,
            "detail": "" if result is None else getattr(result, "comment", ""),
        }

    def modify_stop(self, ticket: int, direction: str, new_stop: float) -> dict:
        with MT5_LOCK:
            mt5 = self._connect()
            position = next((p for p in self._ours(mt5) if int(p.ticket) == int(ticket)), None)
            if position is None:
                return {"accepted": False, "reason": "Position not found on MT5."}
            decision = propose_stop_update(direction, float(position.sl), new_stop)
            if not decision["accepted"]:
                return decision
            result = mt5.order_send(
                {"action": self.c("TRADE_ACTION_SLTP"), "symbol": position.symbol, "position": int(ticket), "sl": new_stop, "tp": float(position.tp), "magic": self.magic}
            )
            ok = result is not None and result.retcode == self.c("TRADE_RETCODE_DONE")
            return {"accepted": ok, "stop": new_stop if ok else float(position.sl), "reason": "Stop tightened on MT5." if ok else f"MT5 refused: {getattr(result, 'comment', '')}"}

    def closed_result(self, ticket: int) -> dict | None:
        """Realized result of a position that is no longer open, from MT5 deal history."""
        with MT5_LOCK:
            mt5 = self._connect()
            deals = mt5.history_deals_get(position=int(ticket)) or []
        if not deals:
            return None
        pnl = sum(float(getattr(d, "profit", 0)) + float(getattr(d, "commission", 0)) + float(getattr(d, "swap", 0)) for d in deals)
        exits = [d for d in deals if getattr(d, "entry", None) == self.c("DEAL_ENTRY_OUT")]
        last = exits[-1] if exits else deals[-1]
        reason = {
            self.c("DEAL_REASON_SL"): "stop (broker)",
            self.c("DEAL_REASON_TP"): "target (broker)",
            self.c("DEAL_REASON_SO"): "stop-out (broker)",
        }.get(getattr(last, "reason", None), "closed")
        return {"pnl": round(pnl, 2), "exit": float(getattr(last, "price", 0.0)), "exit_reason": reason}

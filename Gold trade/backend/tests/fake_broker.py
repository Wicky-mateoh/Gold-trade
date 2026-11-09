"""A stand-in for the MetaTrader5 package that behaves like a broker.

It keeps positions and deal history, honours order_send / order_check /
SL-TP modification, and lets a test hit a stop on the broker side. Real
MT5 only exists on Windows, so every adapter test runs against this.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace


class FakeBroker:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_REJECT = 10006
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_REASON_EXPERT = 3
    DEAL_REASON_SL = 4
    DEAL_REASON_TP = 5
    TIMEFRAME_M5 = 5

    def __init__(self, account_mode=0, algo_trading=True, trade_allowed=True, bid=2649.9, ask=2650.1):
        self.account_mode = account_mode
        self.algo_trading = algo_trading
        self.trade_allowed = trade_allowed
        self.bid, self.ask = bid, ask
        self.positions: dict[int, SimpleNamespace] = {}
        self.deals: list[SimpleNamespace] = []
        self.sent: list[dict] = []
        self.next_ticket = 5001
        self.balance = 10_000.0
        self.reject_next = False
        self.drop_sl_on_fill = False
        self.refuse_sltp = False

    # --- session
    def initialize(self, **kwargs):
        return True

    def shutdown(self):
        pass

    def last_error(self):
        return (0, "ok")

    def account_info(self):
        floating = sum(p.profit for p in self.positions.values())
        return SimpleNamespace(login=777, server="Broker-Demo", trade_mode=self.account_mode, currency="USD",
                               balance=self.balance, equity=self.balance + floating, trade_allowed=self.trade_allowed,
                               trade_expert=True, margin_mode=2)

    def terminal_info(self):
        return SimpleNamespace(name="MetaTrader 5", build=4755, maxbars=100000, trade_allowed=self.algo_trading)

    # --- symbol
    def symbol_info(self, name):
        if name != "XAUUSD":
            return None
        return SimpleNamespace(name=name, visible=True, point=0.01, digits=2, trade_contract_size=100.0,
                               volume_min=0.01, volume_step=0.01, filling_mode=2, trade_stops_level=10)

    def symbols_get(self, pattern):
        return [SimpleNamespace(name="XAUUSD")]

    def symbol_select(self, name, enable):
        return True

    def symbol_info_tick(self, name):
        return SimpleNamespace(bid=self.bid, ask=self.ask, time=int(datetime.now(timezone.utc).timestamp()))

    # --- trading
    def order_check(self, request):
        return SimpleNamespace(retcode=0, comment="ok")

    def order_send(self, request):
        self.sent.append(dict(request))
        if request["action"] == self.TRADE_ACTION_SLTP:
            if self.refuse_sltp:
                return SimpleNamespace(retcode=self.TRADE_RETCODE_REJECT, comment="refused")
            pos = self.positions[request["position"]]
            pos.sl, pos.tp = request["sl"], request["tp"]
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="done")
        if self.reject_next:
            self.reject_next = False
            return SimpleNamespace(retcode=self.TRADE_RETCODE_REJECT, comment="Market closed")
        if "position" in request:  # closing deal
            pos = self.positions.pop(request["position"])
            self._deal(pos, request["price"], self.DEAL_ENTRY_OUT, self.DEAL_REASON_EXPERT)
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=0, price=request["price"], volume=pos.volume, comment="done")
        ticket = self.next_ticket
        self.next_ticket += 1
        pos = SimpleNamespace(ticket=ticket, symbol=request["symbol"], type=request["type"], volume=request["volume"],
                              price_open=request["price"], sl=0.0 if self.drop_sl_on_fill else request["sl"], tp=request["tp"],
                              magic=request["magic"], comment=request["comment"], profit=0.0)
        self.positions[ticket] = pos
        self.deals.append(SimpleNamespace(position_id=ticket, magic=request["magic"], comment=request["comment"], entry=self.DEAL_ENTRY_IN,
                                          price=request["price"], profit=0.0, commission=-0.35, swap=0.0, reason=self.DEAL_REASON_EXPERT))
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=ticket, price=request["price"], volume=request["volume"], comment="done")

    def _deal(self, pos, price, entry, reason):
        sign = 1 if pos.type == self.ORDER_TYPE_BUY else -1
        pnl = round((price - pos.price_open) * sign * pos.volume * 100, 2)
        self.balance += pnl
        self.deals.append(SimpleNamespace(position_id=pos.ticket, magic=pos.magic, comment=pos.comment, entry=entry, price=price,
                                          profit=pnl, commission=-0.35, swap=0.0, reason=reason))

    def hit_stop(self, ticket):
        """Broker-side stop-loss fill."""
        pos = self.positions.pop(ticket)
        self._deal(pos, pos.sl, self.DEAL_ENTRY_OUT, self.DEAL_REASON_SL)

    def positions_get(self, symbol=None, ticket=None):
        rows = list(self.positions.values())
        return [p for p in rows if (symbol is None or p.symbol == symbol) and (ticket is None or p.ticket == ticket)]

    def history_deals_get(self, *args, position=None):
        if position is not None:
            return [d for d in self.deals if d.position_id == position]
        return list(self.deals)

    def copy_rates_from_pos(self, *args):
        return None

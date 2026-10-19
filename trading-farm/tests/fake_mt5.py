"""A small simulated MetaTrader5 module for testing the live bot without Windows/MT5.

Replays 5m bars, keeps pending orders, positions and deals like a broker would:
limit orders fill on touch (at the open if the bar gaps through), stops and
targets are checked bar by bar, and all times handed to the bot are in broker
server time (UTC + offset), exactly like the real terminal.
"""
from types import SimpleNamespace

import numpy as np
import pandas as pd

RATE_DTYPE = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
              ("tick_volume", "<i8"), ("spread", "<i4"), ("real_volume", "<i8")]


class FakeMT5:
    TIMEFRAME_M5 = 5
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 0, 1, 2, 3
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_REMOVE = 1, 5, 6, 8
    ORDER_TIME_GTC, ORDER_TIME_DAY, ORDER_TIME_SPECIFIED, ORDER_TIME_SPECIFIED_DAY = 0, 1, 2, 3
    ORDER_STATE_PLACED, ORDER_STATE_CANCELED, ORDER_STATE_FILLED, ORDER_STATE_EXPIRED = 1, 2, 4, 6
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
    DEAL_REASON_EXPERT, DEAL_REASON_SL, DEAL_REASON_TP = 3, 4, 5

    def __init__(self, bars, server_tz="NY+7", demo=True, balance=10000.0, spread=0.02, login=123,
                 expiration_mode=7):
        """server_tz: "NY+7" (server = New York wall clock + 7 h, switches with US DST) or a
        fixed number of seconds - implemented independently of the bot's own conversion."""
        self.bars = {s: df.reset_index(drop=True) for s, df in bars.items()}
        self.server_tz, self.demo, self.balance, self.spread = server_tz, demo, balance, spread
        self.login, self.expiration_mode = login, expiration_mode
        self.history_orders = {}
        self.now = None
        self.done_until = {s: 0 for s in bars}     # bars processed (index)
        self.orders, self.positions, self.deals = {}, {}, []
        self.next_ticket = 1000
        self.sent = []                             # every order_send request (for assertions)
        self.trade_allowed = True

    # ---------------------------------------------------------------- clock / broker sim
    def offset(self, utc):
        if isinstance(self.server_tz, (int, float)):
            return int(self.server_tz)
        ny = pd.Timestamp(int(utc), unit="s", tz="UTC").tz_convert("America/New_York")
        return int(ny.utcoffset().total_seconds()) + 7 * 3600

    def srv(self, utc):
        return int(utc) + self.offset(utc)

    def _ticket(self):
        self.next_ticket += 1
        return self.next_ticket

    def advance_to(self, now):
        """Let the market run until `now` (UTC seconds): process every bar that closed."""
        self.now = now
        for s, df in self.bars.items():
            i = self.done_until[s]
            while i < len(df) and df.t.iloc[i] + 300 <= now:
                self._process_bar(s, df.iloc[i])
                i += 1
            self.done_until[s] = i

    def _process_bar(self, s, b):
        for tk, o in list(self.orders.items()):
            if o.symbol != s:
                continue
            if o.type_time == self.ORDER_TIME_SPECIFIED and self.srv(b.t) >= o.expiration:
                del self.orders[tk]
                self.history_orders[tk] = SimpleNamespace(ticket=tk, state=self.ORDER_STATE_EXPIRED)
                continue
            buy = o.type == self.ORDER_TYPE_BUY_LIMIT
            if (b.l <= o.price_open) if buy else (b.h >= o.price_open):
                px = min(o.price_open, b.o) if buy else max(o.price_open, b.o)
                del self.orders[tk]
                self.history_orders[tk] = SimpleNamespace(ticket=tk, state=self.ORDER_STATE_FILLED)
                self.positions[tk] = SimpleNamespace(
                    ticket=tk, identifier=tk, symbol=s, magic=o.magic, volume=o.volume_current,
                    type=self.POSITION_TYPE_BUY if buy else self.POSITION_TYPE_SELL,
                    price_open=px, sl=o.sl, tp=o.tp, comment=o.comment, time=self.srv(b.t))
                self._deal(tk, s, o.magic, self.DEAL_ENTRY_IN, px, o.volume_current, 0.0, self.DEAL_REASON_EXPERT, b.t)
        for tk, p in list(self.positions.items()):
            if p.symbol != s:
                continue
            buy = p.type == self.POSITION_TYPE_BUY
            if p.sl and ((b.l <= p.sl) if buy else (b.h >= p.sl)):
                px = min(p.sl, b.o) if buy else max(p.sl, b.o)
                self._close(p, px, self.DEAL_REASON_SL, b.t)
            elif p.tp and ((b.h >= p.tp) if buy else (b.l <= p.tp)):
                self._close(p, p.tp, self.DEAL_REASON_TP, b.t)

    def _deal(self, pos_id, s, magic, entry, px, vol, profit, reason, t):
        self.deals.append(SimpleNamespace(ticket=self._ticket(), position_id=pos_id, symbol=s, magic=magic,
                                          entry=entry, price=px, volume=vol, profit=profit, commission=0.0,
                                          swap=0.0, fee=0.0, reason=reason, comment="", time=self.srv(t)))

    def _close(self, p, px, reason, t):
        sign = 1 if p.type == self.POSITION_TYPE_BUY else -1
        profit = sign * (px - p.price_open) * p.volume
        self.balance += profit
        del self.positions[p.ticket]
        self._deal(p.ticket, p.symbol, p.magic, self.DEAL_ENTRY_OUT, px, p.volume, profit, reason, t)

    def _last_close(self, s):
        df = self.bars[s]
        i = self.done_until[s]
        return float(df.c.iloc[i - 1]) if i > 0 else float(df.o.iloc[0])

    # ---------------------------------------------------------------- MT5 API
    def initialize(self, **kw):
        return True

    def shutdown(self):
        pass

    def last_error(self):
        return (1, "Success")

    def terminal_info(self):
        return SimpleNamespace(connected=True, trade_allowed=self.trade_allowed)

    def account_info(self):
        floating = 0.0
        for p in self.positions.values():
            sign = 1 if p.type == self.POSITION_TYPE_BUY else -1
            floating += sign * (self._last_close(p.symbol) - p.price_open) * p.volume
        eq = self.balance + floating
        return SimpleNamespace(login=self.login, server="Fake-Demo", currency="USD", balance=self.balance, equity=eq,
                               margin_free=eq, trade_mode=self.ACCOUNT_TRADE_MODE_DEMO if self.demo
                               else self.ACCOUNT_TRADE_MODE_REAL)

    def symbol_info(self, s):
        if s not in self.bars:
            return None
        return SimpleNamespace(name=s, visible=True, digits=2, point=0.01, trade_tick_size=0.01, volume_min=1.0,
                               volume_max=100000.0, volume_step=1.0, trade_stops_level=0, trade_freeze_level=0,
                               filling_mode=1, trade_mode=4, expiration_mode=self.expiration_mode)

    def symbol_select(self, s, enable=True):
        return s in self.bars

    def symbols_get(self, group="*"):
        return [SimpleNamespace(name=s) for s in self.bars]

    def symbol_info_tick(self, s):
        if s not in self.bars or self.now is None:
            return None
        bid = self._last_close(s)
        return SimpleNamespace(time=self.srv(self.now), bid=bid, ask=bid + self.spread, last=bid)

    def copy_rates_from_pos(self, s, tf, start, count):
        df = self.bars[s]
        df = df[df.t <= self.now].tail(count)   # includes the still-forming bar
        out = np.zeros(len(df), dtype=RATE_DTYPE)
        out["time"] = [self.srv(t) for t in df.t.values]
        for src, dst in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close")):
            out[dst] = df[src].values
        out["tick_volume"] = df.v.values
        return out

    def positions_get(self, symbol=None):
        return tuple(p for p in self.positions.values() if symbol is None or p.symbol == symbol)

    def orders_get(self, symbol=None):
        return tuple(o for o in self.orders.values() if symbol is None or o.symbol == symbol)

    def history_deals_get(self, *args, position=None, **kw):
        return tuple(d for d in self.deals if position is None or d.position_id == position)

    def history_orders_get(self, ticket=None, **kw):
        o = self.history_orders.get(ticket)
        return (o,) if o else ()

    def order_calc_profit(self, otype, s, vol, p_open, p_close):
        sign = 1 if otype == self.ORDER_TYPE_BUY else -1
        return sign * (p_close - p_open) * vol

    def order_calc_margin(self, otype, s, vol, price):
        return 0.25 * vol * price

    def order_send(self, req):
        self.sent.append(dict(req))
        res = lambda code, order=0: SimpleNamespace(retcode=code, order=order, deal=0, price=req.get("price", 0.0),
                                                    comment="")
        a = req["action"]
        if a == self.TRADE_ACTION_PENDING:
            if req.get("type_filling") != self.ORDER_FILLING_RETURN:
                return res(10030)
            tick = self.symbol_info_tick(req["symbol"])
            buy = req["type"] == self.ORDER_TYPE_BUY_LIMIT
            if (buy and req["price"] >= tick.ask) or (not buy and req["price"] <= tick.bid):
                return res(10015)  # invalid price
            tk = self._ticket()
            self.orders[tk] = SimpleNamespace(ticket=tk, symbol=req["symbol"], magic=req["magic"], type=req["type"],
                                              price_open=req["price"], sl=req["sl"], tp=req["tp"],
                                              volume_current=req["volume"], comment=req.get("comment", ""),
                                              time_setup=self.srv(self.now), type_time=req.get("type_time", 0),
                                              expiration=req.get("expiration", 0))
            return res(10008, tk)
        if a == self.TRADE_ACTION_REMOVE:
            if self.orders.pop(req["order"], None) is None:
                return res(10013)
            self.history_orders[req["order"]] = SimpleNamespace(ticket=req["order"], state=self.ORDER_STATE_CANCELED)
            return res(10009)
        if a == self.TRADE_ACTION_SLTP:
            p = self.positions.get(req["position"])
            if p is None:
                return res(10013)
            p.sl, p.tp = req["sl"], req["tp"]
            return res(10009)
        if a == self.TRADE_ACTION_DEAL:
            if req.get("type_filling") != self.ORDER_FILLING_FOK:
                return res(10030)
            p = self.positions.get(req.get("position"))
            if p is None:
                return res(10013)
            self._close(p, req["price"], self.DEAL_REASON_EXPERT, self.now - self.now % 300)
            return res(10009)
        return res(10013)

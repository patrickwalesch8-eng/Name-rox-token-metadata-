"""
MT5-Autopilot der Aktien Data Farm - SMC-Engine (Sweep -> MSS -> FVG/OB) auf US-Aktien-CFDs.

Ablauf bei jeder neu geschlossenen 5-Minuten-Kerze (reguläre US-Handelszeit):
  1. Kerzen vom MT5-Terminal holen, Broker-Serverzeit in New-York-Zeit umrechnen
  2. Dieselbe Erkennung + Engine wie im Dashboard laufen lassen (farmlib)
  3. Hat die Engine GENAU AUF DIESER KERZE ein Setup scharf gemacht:
     Limit-Order beim Broker MIT Stop-Loss und Take-Profit platzieren
  4. Hat die Engine ein Setup verworfen (kein Retest, weggelaufen, ...):
     die passende Limit-Order beim Broker löschen
  5. Mit der Kerze, die um flat_at (15:55 New York) endet: alles glattstellen

Sicherheit (Konzept vom Trend-Bot übernommen und erweitert):
  - startet nicht auf Echtgeldkonten (ALLOW_REAL_ACCOUNT)
  - jede Position hat SL/TP beim Broker; fehlt etwas -> nachrüsten, sonst schließen
  - Lotgröße rechnet der Broker selbst (order_calc_profit), abgerundet
  - max. eine Position/Order pro Aktie, MAX_OPEN_POSITIONS gesamt
  - Tagesverlust-Grenze, Spread-Filter, Neustart-fest (Zustand in einer Datei)
  - beim Start werden keine alten Signale nachgehandelt

Start:  python bot_smc.py      Stopp:  Strg + C (SL/TP bleiben beim Broker aktiv)
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import mt5_config as C  # noqa: E402
from farmlib import data  # noqa: E402
from farmlib.engine import hhmm  # noqa: E402
from farmlib.pipeline import analyze  # noqa: E402

log = logging.getLogger("bot_smc")

JOURNAL_COLS = ["zeit_ny", "ereignis", "markt", "symbol", "richtung", "lots", "entry", "sl", "tp",
                "exit", "risiko", "gewinn", "r", "gebuehren", "grund", "setup", "kapital"]

RET_PLACED, RET_DONE, RET_DONE_PARTIAL, RET_INVALID_FILL = 10008, 10009, 10010, 10030
OK_RETCODES = (RET_PLACED, RET_DONE, RET_DONE_PARTIAL)


def setup_logging():
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    for h in (logging.FileHandler(HERE / C.LOG_FILE, encoding="utf-8"), logging.StreamHandler()):
        h.setFormatter(fmt)
        log.addHandler(h)


def ny_now(ts: float) -> datetime:
    return pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(data.NY).to_pydatetime()


class SmcBot:
    def __init__(self, mt5, clock=time.time, cfg=None, workdir: Path = HERE):
        self.mt5 = mt5
        self.clock = clock
        self.cfg = cfg or json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.eng = self.cfg["engine"]
        self.flat_m = hhmm(self.eng.get("flat_at", "16:00"))
        self.workdir = Path(workdir)
        self.symbols: dict[str, str] = {}       # farm name -> broker symbol
        self.last_bar: dict[str, int] = {}      # farm name -> newest processed bar (UTC)
        self.status: dict[str, str] = {}
        self.offset = None                      # broker server time - UTC, seconds
        self.known_positions: dict[int, dict] = {}
        self.last_heartbeat = 0.0
        self.last_flat_try = 0.0
        self.trading_ok = True
        self.state = {"plans": {}, "day": None, "day_equity": None, "kill": False}
        self.load_state()

    # ------------------------------------------------------------------ state / journal
    def path(self, name):
        return self.workdir / name

    def load_state(self):
        p = self.path(C.STATE_FILE)
        if p.exists():
            try:
                self.state.update(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                log.warning(f"Zustandsdatei unlesbar ({exc}) - starte mit leerem Zustand.")

    def save_state(self):
        p = self.path(C.STATE_FILE)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    def journal(self, row: dict):
        p = self.path(C.JOURNAL_FILE)
        new = not p.exists()
        with open(p, "a", encoding="utf-8") as f:
            if new:
                f.write(";".join(JOURNAL_COLS) + "\n")
            row.setdefault("zeit_ny", ny_now(self.clock()).strftime("%Y-%m-%d %H:%M:%S"))
            f.write(";".join(str(row.get(k, "")) for k in JOURNAL_COLS) + "\n")

    # ------------------------------------------------------------------ connection
    def connect(self):
        m = self.mt5
        kwargs = {}
        if C.MT5_LOGIN:
            kwargs = dict(login=C.MT5_LOGIN, password=C.MT5_PASSWORD, server=C.MT5_SERVER)
        if not m.initialize(**kwargs):
            log.error(f"MT5-Initialisierung fehlgeschlagen: {m.last_error()}")
            log.error("Ist das MT5-Terminal gestartet und eingeloggt?")
            sys.exit(1)
        acc = m.account_info()
        if acc is None:
            log.error("Kein Konto gefunden - im MT5-Terminal einloggen.")
            m.shutdown()
            sys.exit(1)
        is_demo = acc.trade_mode == m.ACCOUNT_TRADE_MODE_DEMO
        log.info(f"Verbunden: Konto {acc.login} ({acc.server}), {'DEMO' if is_demo else 'ECHTGELD'}, "
                 f"Kapital {acc.equity:.2f} {acc.currency}")
        if not is_demo and not C.ALLOW_REAL_ACCOUNT:
            log.error("SICHERHEITSSTOPP: Dies ist ein ECHTGELDKONTO. Der Bot ist für Demo-Betrieb "
                      "konfiguriert (ALLOW_REAL_ACCOUNT = False).")
            m.shutdown()
            sys.exit(1)
        self.ensure_symbols()

    def ensure_connection(self) -> bool:
        m = self.mt5
        term = m.terminal_info()
        if term is None or not getattr(term, "connected", True):
            log.warning("Verbindung zum MT5-Terminal verloren - versuche neu zu verbinden ...")
            m.shutdown()
            if not m.initialize():
                return False
            acc = m.account_info()
            if acc is None or (acc.trade_mode != m.ACCOUNT_TRADE_MODE_DEMO and not C.ALLOW_REAL_ACCOUNT):
                log.error("Nach dem Neuverbinden kein (Demo-)Konto - kein Handel.")
                return False
            term = m.terminal_info()
            if term is None:
                return False
        allowed = bool(getattr(term, "trade_allowed", True))
        if allowed != self.trading_ok:
            if allowed:
                log.info("Algo-Handel im Terminal ist wieder erlaubt.")
            else:
                log.error("Algo-Handel ist im MT5-Terminal AUS (Knopf 'Algo Trading' oben) - "
                          "es werden keine neuen Orders platziert.")
        self.trading_ok = allowed
        return True

    def ensure_symbols(self):
        m = self.mt5
        for key, sym in C.MARKETS.items():
            info = m.symbol_info(sym)
            if info is None:
                hints = [s.name for s in (m.symbols_get(f"*{key}*") or [])][:8]
                log.warning(f"{key}: Symbol '{sym}' beim Broker nicht gefunden - übersprungen."
                            + (f" Mögliche Namen: {', '.join(hints)}" if hints else ""))
                continue
            if not info.visible:
                m.symbol_select(sym, True)
            self.symbols[key] = sym
            log.info(f"{key}: aktiv als '{sym}'")
        for sym in C.CLOCK_SYMBOLS:
            if m.symbol_info(sym) is not None:
                m.symbol_select(sym, True)
        if not self.symbols:
            log.error("Keines der konfigurierten Symbole verfügbar - Abbruch.")
            m.shutdown()
            sys.exit(1)

    # ------------------------------------------------------------------ time & data
    def update_offset(self):
        """Broker-Serverzeit minus UTC. MT5 liefert Zeiten in Serverzeit."""
        if C.SERVER_UTC_OFFSET_HOURS is not None:
            self.offset = int(C.SERVER_UTC_OFFSET_HOURS * 3600)
            return
        newest = None
        for sym in list(self.symbols.values()) + list(C.CLOCK_SYMBOLS):
            tick = self.mt5.symbol_info_tick(sym)
            if tick is not None and tick.time and (newest is None or tick.time > newest):
                newest = tick.time
        if newest is None:
            return
        cand = round((newest - self.clock()) / 1800) * 1800
        # ein frischer Tick liegt höchstens ein paar Minuten zurück; sonst Wert behalten
        if abs(cand) <= 14 * 3600 and abs((newest - self.clock()) - cand) <= 600:
            if cand != self.offset:
                log.info(f"Broker-Serverzeit = UTC{cand / 3600:+.1f} h")
            self.offset = cand

    def get_bars(self, sym: str) -> pd.DataFrame | None:
        """Geschlossene 5m-Kerzen der regulären US-Handelszeit, Zeit in UTC-Sekunden."""
        if self.offset is None:
            return None
        rates = self.mt5.copy_rates_from_pos(sym, self.mt5.TIMEFRAME_M5, 0, C.BARS_HISTORY)
        if rates is None or len(rates) < 50:
            return None
        df = pd.DataFrame({
            "t": rates["time"].astype("int64") - self.offset,
            "o": rates["open"].astype(float), "h": rates["high"].astype(float),
            "l": rates["low"].astype(float), "c": rates["close"].astype(float),
            "v": rates["tick_volume"].astype("int64"),
        })
        df = df[df.t + 300 <= self.clock()]          # noch laufende Kerze weg
        df = data.add_session_cols(df)
        df = df[(df.mins >= data.SESSION_OPEN_MIN) & (df.mins < data.SESSION_CLOSE_MIN)]
        df = df.drop_duplicates("t").sort_values("t").reset_index(drop=True)
        return df if len(df) >= 50 else None

    # ------------------------------------------------------------------ broker helpers
    def my_positions(self, sym=None):
        res = self.mt5.positions_get(symbol=sym) if sym else self.mt5.positions_get()
        return [p for p in (res or []) if p.magic == C.MAGIC_NUMBER]

    def my_orders(self, sym=None):
        res = self.mt5.orders_get(symbol=sym) if sym else self.mt5.orders_get()
        return [o for o in (res or []) if o.magic == C.MAGIC_NUMBER]

    def fillings(self, info, pending=False):
        m = self.mt5
        modes = []
        if pending:
            modes.append(m.ORDER_FILLING_RETURN)
        fm = int(getattr(info, "filling_mode", 0) or 0)
        if fm & 1:
            modes.append(m.ORDER_FILLING_FOK)
        if fm & 2:
            modes.append(m.ORDER_FILLING_IOC)
        if not pending:
            modes.append(m.ORDER_FILLING_RETURN)
        return list(dict.fromkeys(modes)) or [m.ORDER_FILLING_RETURN]

    def send(self, request: dict, fillings):
        """Sendet eine Order. Nur bei 'falscher Füllart' wird mit der nächsten Füllart
        wiederholt - nie nach einer (Teil-)Ausführung, damit nichts doppelt öffnet."""
        res = None
        for f in fillings:
            res = self.mt5.order_send(dict(request, type_filling=f))
            if res is None or res.retcode != RET_INVALID_FILL:
                break
        return res

    def round_price(self, info, x):
        ts = info.trade_tick_size or info.point
        return round(round(x / ts) * ts, info.digits)

    def calc_lots(self, sym, info, long: bool, entry: float, sl: float):
        """Lotgröße über die Broker-Berechnung (order_calc_profit), immer abgerundet.

        Rückgabe: (lots, tatsächliches Risiko in Kontowährung) - oder ein Text mit dem
        Grund, warum nicht gehandelt wird. Es wird nie MEHR als geplant riskiert; reicht
        die Margin nicht für das volle Risiko, wird die Größe verkleinert.
        """
        m = self.mt5
        acc = m.account_info()
        if acc is None:
            return "keine Kontodaten"
        risk_amount = acc.equity * C.RISK_PERCENT / 100
        otype = m.ORDER_TYPE_BUY if long else m.ORDER_TYPE_SELL
        step = info.volume_step or 0.01

        def loss_for(vol):
            r = m.order_calc_profit(otype, sym, vol, entry, sl)
            return None if r is None else abs(r)

        def floor_step(x):
            return round(math.floor(x / step + 1e-9) * step, 8)

        per_lot = loss_for(1.0)
        if not per_lot:
            return f"order_calc_profit fehlgeschlagen ({m.last_error()})"
        lots = min(floor_step(risk_amount / per_lot), info.volume_max)
        note = ""
        margin_per_lot = m.order_calc_margin(otype, sym, 1.0, entry) if hasattr(m, "order_calc_margin") else None
        if margin_per_lot:
            max_lots = floor_step(acc.margin_free * 0.9 / margin_per_lot)
            if max_lots < lots:
                lots, note = max_lots, " (wegen Margin verkleinert)"
        if lots < info.volume_min:
            min_risk = loss_for(info.volume_min)
            min_margin = margin_per_lot * info.volume_min if margin_per_lot else 0
            if min_risk is None or min_risk > 1.5 * risk_amount or min_margin > acc.margin_free * 0.9:
                return (f"schon das Mindest-Lot {info.volume_min} wäre zu viel Risiko oder Margin "
                        f"(Risiko {min_risk if min_risk is not None else '?'} bei Ziel {risk_amount:.2f})")
            lots = info.volume_min
        actual = loss_for(lots)
        if actual is None or actual > 1.5 * risk_amount:
            return f"Risikoprüfung fehlgeschlagen ({actual} bei Ziel {risk_amount:.2f})"
        if note:
            log.info(f"{sym}: Lotgröße {lots}{note} - Risiko {actual:.2f} statt {risk_amount:.2f}.")
        return lots, actual

    def close_position(self, pos, why: str) -> bool:
        m = self.mt5
        info, tick = m.symbol_info(pos.symbol), m.symbol_info_tick(pos.symbol)
        if info is None or tick is None:
            log.error(f"{pos.symbol}: Position {pos.ticket} kann nicht geschlossen werden (keine Kurse).")
            return False
        is_buy = pos.type == m.POSITION_TYPE_BUY
        req = {"action": m.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume,
               "type": m.ORDER_TYPE_SELL if is_buy else m.ORDER_TYPE_BUY, "position": pos.ticket,
               "price": tick.bid if is_buy else tick.ask, "deviation": 50, "magic": C.MAGIC_NUMBER,
               "comment": f"ADF {why}"[:31], "type_time": m.ORDER_TIME_GTC}
        res = self.send(req, self.fillings(info))
        if res is not None and res.retcode in (RET_DONE, RET_DONE_PARTIAL):
            log.info(f"{pos.symbol}: Position {pos.ticket} geschlossen ({why}).")
            return True
        log.error(f"{pos.symbol}: Schließen von {pos.ticket} FEHLGESCHLAGEN (Retcode "
                  f"{res.retcode if res else m.last_error()}) - neuer Versuch in der nächsten Runde.")
        return False

    def cancel_order(self, order, why: str) -> bool:
        m = self.mt5
        res = m.order_send({"action": m.TRADE_ACTION_REMOVE, "order": order.ticket})
        ok = res is not None and res.retcode in (RET_DONE, RET_PLACED)
        plan = self.state["plans"].get(str(order.ticket), {})
        if ok:
            log.info(f"{order.symbol}: Limit-Order {order.ticket} gelöscht ({why}).")
            self.journal({"ereignis": "STORNO", "markt": plan.get("key", ""), "symbol": order.symbol,
                          "richtung": plan.get("dir", ""), "entry": plan.get("entry", ""), "grund": why})
            plan["status"] = "storniert"
            self.save_state()
        else:
            log.error(f"{order.symbol}: Löschen von Order {order.ticket} fehlgeschlagen "
                      f"({res.retcode if res else m.last_error()}).")
        return ok

    # ------------------------------------------------------------------ daily guard
    def daily_guard(self):
        acc = self.mt5.account_info()
        if acc is None:
            return
        today = ny_now(self.clock()).strftime("%Y-%m-%d")
        if self.state["day"] != today:
            self.state.update(day=today, day_equity=acc.equity, kill=False)
            self.save_state()
        limit = self.state["day_equity"] * (1 - C.DAILY_LOSS_LIMIT_PERCENT / 100)
        if not self.state["kill"] and acc.equity <= limit:
            self.state["kill"] = True
            self.save_state()
            log.error(f"TAGESVERLUST-GRENZE erreicht (Kapital {acc.equity:.2f} <= {limit:.2f}). "
                      f"Heute keine neuen Orders mehr; wartende Limit-Orders werden gelöscht.")
            for o in self.my_orders():
                self.cancel_order(o, "Tagesverlust-Grenze")

    def flat_guard(self):
        """Außerhalb von 09:30 bis flat_at (New York) hält der Bot nichts - unabhängig davon,
        ob noch Kerzen kommen. Schlägt das Schließen fehl, alle 5 Minuten neuer Versuch."""
        nyt = ny_now(self.clock())
        mins = nyt.hour * 60 + nyt.minute
        if nyt.weekday() < 5 and data.SESSION_OPEN_MIN <= mins < self.flat_m:
            return
        if self.clock() - self.last_flat_try < 300:
            return
        pos, ords = self.my_positions(), self.my_orders()
        if not pos and not ords:
            return
        self.last_flat_try = self.clock()
        for o in ords:
            self.cancel_order(o, "außerhalb der Handelszeit")
        for p in pos:
            self.close_position(p, "Schluss")

    # ------------------------------------------------------------------ fills, closes, protection
    def track_positions(self):
        m = self.mt5
        current = {p.ticket: p for p in self.my_positions()}
        for ticket, pos in current.items():
            if ticket in self.known_positions:
                continue
            plan = self.state["plans"].get(str(pos.identifier), {})
            self.known_positions[ticket] = {"symbol": pos.symbol, "plan_id": str(pos.identifier)}
            if plan and plan.get("status") != "gefüllt":
                plan["status"] = "gefüllt"
                self.save_state()
                log.info(f"⭐ EINSTIEG: {plan.get('dir', '').upper()} {pos.symbol} {pos.volume} @ {pos.price_open} "
                         f"| SL {pos.sl} | TP {pos.tp}")
                self.journal({"ereignis": "OPEN", "markt": plan.get("key"), "symbol": pos.symbol,
                              "richtung": plan.get("dir"), "lots": pos.volume, "entry": pos.price_open,
                              "sl": pos.sl, "tp": pos.tp, "risiko": f"{plan.get('risk_money', 0):.2f}",
                              "setup": plan.get("chain", ""), "kapital": self.equity_txt()})
        for ticket in [t for t in self.known_positions if t not in current]:
            info = self.known_positions.pop(ticket)
            self.journal_close(ticket, info)

    def journal_close(self, ticket, info):
        m = self.mt5
        now = self.clock() + (self.offset or 0)
        deals = None
        try:
            deals = m.history_deals_get(position=ticket)
        except TypeError:
            pass
        if not deals:
            deals = [d for d in (m.history_deals_get(datetime.fromtimestamp(now - 7 * 86400, timezone.utc),
                                                     datetime.fromtimestamp(now + 86400, timezone.utc)) or [])
                     if d.position_id == ticket]
        outs = [d for d in deals if d.entry in (m.DEAL_ENTRY_OUT, m.DEAL_ENTRY_OUT_BY)]
        if not outs:
            return
        profit = sum(d.profit for d in outs)
        fees = sum(d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals)
        net = profit + fees
        plan = self.state["plans"].get(info["plan_id"], {})
        risk = plan.get("risk_money") or 0
        r_txt = f"{net / risk:+.2f}" if risk else ""
        reasons = {getattr(m, "DEAL_REASON_SL", 4): "Stop-Loss", getattr(m, "DEAL_REASON_TP", 5): "Take-Profit"}
        why = reasons.get(getattr(outs[-1], "reason", -1), outs[-1].comment or "geschlossen")
        log.info(f"{'🟢' if net >= 0 else '🔴'} GESCHLOSSEN: {info['symbol']} @ {outs[-1].price} | "
                 f"{net:+.2f} ({r_txt}R) | {why}")
        self.journal({"ereignis": "CLOSE", "markt": plan.get("key"), "symbol": info["symbol"],
                      "richtung": plan.get("dir"), "exit": outs[-1].price, "gewinn": f"{net:.2f}",
                      "r": r_txt, "gebuehren": f"{fees:.2f}", "grund": why, "kapital": self.equity_txt()})
        plan["status"] = "geschlossen"
        self.save_state()

    def guard_protection(self):
        """Jede Position MUSS SL und TP beim Broker haben."""
        m = self.mt5
        for pos in self.my_positions():
            if pos.sl > 0 and pos.tp > 0:
                continue
            plan = self.state["plans"].get(str(pos.identifier), {})
            if plan.get("sl") and plan.get("tp"):
                log.warning(f"⚠ {pos.symbol}: Position {pos.ticket} ohne vollständigen Schutz - rüste nach ...")
                res = m.order_send({"action": m.TRADE_ACTION_SLTP, "symbol": pos.symbol, "position": pos.ticket,
                                    "sl": plan["sl"], "tp": plan["tp"], "magic": C.MAGIC_NUMBER})
                if res is not None and res.retcode == RET_DONE:
                    log.info(f"✔ {pos.symbol}: Schutz nachgerüstet (SL {plan['sl']} / TP {plan['tp']}).")
                    continue
            log.error(f"✖ {pos.symbol}: Position {pos.ticket} ungeschützt - wird geschlossen.")
            self.close_position(pos, "ohne SL/TP")

    # ------------------------------------------------------------------ the strategy per symbol
    def process_symbol(self, key: str, sym: str):
        df = self.get_bars(sym)
        if df is None:
            return
        newest = int(df.t.iloc[-1])
        prev = self.last_bar.get(key)
        if prev == newest:
            return
        self.last_bar[key] = newest
        first = prev is None
        flat = int(df.mins.iloc[-1]) + 5 >= self.flat_m

        live_pos, live_ord = self.my_positions(sym), self.my_orders(sym)
        if flat:
            for o in live_ord:
                self.cancel_order(o, "Sessionende")
            for p in live_pos:
                self.close_position(p, "Schluss")
            self.status[key] = "Schluss - glattgestellt"
            return

        res = analyze(df, key, self.cfg, open_end=True)
        st = res["state"]
        eng_ord = st.get("order")
        self.status[key] = ("im Trade" if live_pos else "Limit aktiv" if live_ord else
                            "Sweep - warte auf MSS" if st.get("cand") else "beobachte")

        # 1) Limit-Orders löschen, die die Engine nicht mehr hält
        max_age = (self.eng["entry_window"] + 2) * 300
        for o in live_ord:
            plan = self.state["plans"].get(str(o.ticket), {})
            same = eng_ord is not None and plan.get("armed_t") == eng_ord["armed_t"]
            too_old = self.clock() - plan.get("placed_utc", self.clock()) > max_age
            if not same or too_old:
                why = "zu alt" if same else self.why_dropped(res, plan.get("armed_t"))
                self.cancel_order(o, why)

        # 2) neues Setup genau auf dieser Kerze -> Limit-Order
        if first or eng_ord is None or eng_ord["armed_t"] != newest:
            return
        self.try_place(key, sym, eng_ord)

    def why_dropped(self, res, armed_t):
        for r in res["refusals"]:
            if r.get("armed_t") == armed_t:
                return r["reason"]
        if any(t.get("armed_t") == armed_t for t in res["trades"]):
            return "in der Simulation gefüllt, beim Broker nicht"
        return "Setup nicht mehr gültig"

    def try_place(self, key, sym, o):
        m = self.mt5
        long = o["dir"] == "long"
        chain = (f"Sweep {o['sweep_lb']} {o['sweep_p']} -> {o['mss_k'].upper()} {o['mss_p']} -> "
                 f"{o['zone']['type']} {o['zone']['bot']}-{o['zone']['top']}")
        head = f"{key} {o['dir'].upper()}-Setup ({chain})"

        def skip(reason):
            log.info(f"{head}: NICHT platziert - {reason}")
            self.journal({"ereignis": "SKIP", "markt": key, "symbol": sym, "richtung": o["dir"],
                          "entry": o["entry"], "sl": o["sl"], "tp": o["tp"], "grund": reason, "setup": chain})

        if not self.trading_ok:
            return skip("Algo-Handel im Terminal aus")
        if self.state.get("kill"):
            return skip("Tagesverlust-Grenze erreicht")
        if self.my_positions(sym) or self.my_orders(sym):
            return skip("schon Position/Order in dieser Aktie")
        if len(self.my_positions()) + len(self.my_orders()) >= C.MAX_OPEN_POSITIONS:
            return skip(f"max. {C.MAX_OPEN_POSITIONS} Positionen/Orders erreicht")
        info, tick = m.symbol_info(sym), m.symbol_info_tick(sym)
        if info is None or tick is None or not tick.bid or not tick.ask:
            return skip("keine aktuellen Kurse")
        tm = getattr(info, "trade_mode", 4)
        if tm in (0, 3) or (tm == 1 and not long) or (tm == 2 and long):
            return skip("Handel in dieser Richtung beim Broker nicht erlaubt")

        entry, sl, tp = (self.round_price(info, x) for x in (o["entry"], o["sl"], o["tp"]))
        risk = abs(entry - sl)
        if risk <= 0:
            return skip("Stop-Abstand 0")
        spread = tick.ask - tick.bid
        if spread > C.MAX_SPREAD_OF_RISK * risk:
            return skip(f"Spread {spread:.4f} zu groß für Stop-Abstand {risk:.4f}")
        gap = max(info.trade_stops_level or 0, info.trade_freeze_level or 0) * info.point
        if long and not (entry < tick.ask - gap and sl < entry - gap and tp > entry + gap):
            return skip(f"Kurs (Ask {tick.ask}) schon in der Zone oder Abstände unter Broker-Minimum")
        if not long and not (entry > tick.bid + gap and sl > entry + gap and tp < entry - gap):
            return skip(f"Kurs (Bid {tick.bid}) schon in der Zone oder Abstände unter Broker-Minimum")
        sized = self.calc_lots(sym, info, long, entry, sl)
        if isinstance(sized, str):
            return skip(sized)
        lots, risk_money = sized

        req = {"action": m.TRADE_ACTION_PENDING, "symbol": sym, "volume": lots,
               "type": m.ORDER_TYPE_BUY_LIMIT if long else m.ORDER_TYPE_SELL_LIMIT,
               "price": entry, "sl": sl, "tp": tp, "magic": C.MAGIC_NUMBER,
               "comment": f"ADF {o['armed_t']}"[:31], "type_time": m.ORDER_TIME_GTC}
        res = self.send(req, self.fillings(info, pending=True))
        if res is None or res.retcode not in OK_RETCODES:
            return skip(f"Broker hat abgelehnt (Retcode {res.retcode if res else m.last_error()})")
        self.state["plans"][str(res.order)] = {
            "key": key, "symbol": sym, "dir": o["dir"], "armed_t": o["armed_t"], "entry": entry,
            "sl": sl, "tp": tp, "lots": lots, "risk_money": risk_money, "chain": chain,
            "placed_utc": self.clock(), "status": "wartet"}
        self.prune_plans()
        self.save_state()
        log.info(f"★ {head}: Limit {lots} @ {entry} | SL {sl} | TP {tp} | Risiko {risk_money:.2f}")
        self.journal({"ereignis": "ORDER", "markt": key, "symbol": sym, "richtung": o["dir"], "lots": lots,
                      "entry": entry, "sl": sl, "tp": tp, "risiko": f"{risk_money:.2f}", "setup": chain,
                      "kapital": self.equity_txt()})

    def prune_plans(self, keep_days=14):
        cutoff = self.clock() - keep_days * 86400
        plans = self.state["plans"]
        for k in [k for k, v in plans.items() if v.get("status") in ("geschlossen", "storniert")
                  and v.get("placed_utc", 0) < cutoff]:
            del plans[k]

    # ------------------------------------------------------------------ loop
    def equity_txt(self):
        acc = self.mt5.account_info()
        return f"{acc.equity:.2f}" if acc else ""

    def heartbeat(self, force=False):
        if not force and self.clock() - self.last_heartbeat < C.HEARTBEAT_MINUTES * 60:
            return
        self.last_heartbeat = self.clock()
        acc = self.mt5.account_info()
        eq = f"Kapital {acc.equity:.2f} {acc.currency}" if acc else "Kapital ?"
        log.info(f"♥ {ny_now(self.clock()):%H:%M} New York - {eq}, {len(self.my_positions())} Position(en), "
                 f"{len(self.my_orders())} Limit-Order(s){' - TAGESGRENZE AKTIV' if self.state.get('kill') else ''}")
        for key in self.symbols:
            log.info(f"   {key:<6} {self.status.get(key, 'warte auf Daten / Markt geschlossen')}")

    def step(self):
        if not self.ensure_connection():
            return
        self.update_offset()
        self.track_positions()
        self.guard_protection()
        self.daily_guard()
        self.flat_guard()
        for key, sym in self.symbols.items():
            try:
                self.process_symbol(key, sym)
            except Exception:  # ein Markt darf den Bot nicht stoppen
                log.exception(f"{key}: Fehler bei der Verarbeitung")
        self.heartbeat()

    def run(self):
        log.info("=" * 64)
        log.info("Aktien Data Farm - MT5-Autopilot (SMC-Engine) startet")
        log.info(f"Strategie aus config.json: RR {self.eng['target_rr']}, Einstiege "
                 f"{self.eng['no_entry_before']}-{self.eng['no_entry_after']} NY, glatt um {self.eng.get('flat_at')} NY, "
                 f"Risiko {C.RISK_PERCENT}%/Trade, Tagesgrenze -{C.DAILY_LOSS_LIMIT_PERCENT}%")
        self.connect()
        self.known_positions = {p.ticket: {"symbol": p.symbol, "plan_id": str(p.identifier)}
                                for p in self.my_positions()}
        log.info("Warte auf neue Kerzen ... (Stopp: Strg + C - SL/TP bleiben beim Broker aktiv)")
        try:
            while True:
                self.step()
                time.sleep(C.POLL_SECONDS)
        except KeyboardInterrupt:
            for o in self.my_orders():  # ohne Bot würde niemand sie rechtzeitig löschen
                self.cancel_order(o, "Bot gestoppt")
            log.info("Bot gestoppt. Wartende Limit-Orders wurden gelöscht. Offene Positionen sind durch "
                     "SL/TP beim Broker gesichert - aber ohne Bot werden sie NICHT zum Schluss glattgestellt!")
        finally:
            self.mt5.shutdown()


def main():
    setup_logging()
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("FEHLER: MetaTrader5-Paket fehlt. Installieren mit:  pip install MetaTrader5")
        sys.exit(1)
    SmcBot(mt5).run()


if __name__ == "__main__":
    main()

"""
MT5-Autopilot der Aktien Data Farm - SMC-Engine (Sweep -> MSS -> FVG/OB) auf US-Aktien-CFDs.

Ablauf bei jeder neu geschlossenen 5-Minuten-Kerze (reguläre US-Handelszeit):
  1. Kerzen vom MT5-Terminal holen; Broker-Serverzeit pro Kerze (sommer-/winterzeitfest)
     in New-York-Zeit umrechnen
  2. Mustererkennung + Engine laufen lassen (farmlib, Einstellungen aus ../config.json)
  3. Hat die Engine GENAU AUF DIESER KERZE ein Setup scharf gemacht:
     Limit-Order beim Broker MIT Stop-Loss, Take-Profit und Ablaufzeit platzieren
  4. Hält die Engine das Setup nicht mehr (kein Retest, weggelaufen, ...):
     die passende Limit-Order beim Broker löschen
  5. Mit der Kerze, die um flat_at (15:55 New York; an halben Tagen 12:55) endet:
     alles glattstellen

Sicherheit (Konzept vom Trend-Bot übernommen und erweitert):
  - handelt nur auf dem Demokonto, mit dem er gestartet wurde (auch nach Kontowechsel/Neuverbinden)
  - jede Position hat SL/TP beim Broker; fehlt etwas -> nachrüsten, sonst schließen
  - Limit-Orders verfallen beim Broker von selbst, auch wenn der Bot abstürzt
  - Lotgröße rechnet der Broker selbst (order_calc_profit), abgerundet, durch Margin begrenzt
  - max. eine Position/Order pro Aktie, MAX_OPEN_POSITIONS gesamt; nie Doppel-Orders
  - Tagesverlust-Grenze, Spread-Filter, nur eine Instanz gleichzeitig
  - Serverzeit-Regel und PC-Uhr werden an frischen Ticks geprüft; glattgestellt wird nach der Broker-Uhr
  - ein Fehler (z.B. Journal in Excel geöffnet) stoppt den Bot nicht
  - beim Start werden keine alten Signale nachgehandelt

Start:  python bot_smc.py      Stopp:  Strg + C (SL/TP bleiben beim Broker aktiv)
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
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

# Rückgabecodes von order_send (MQL5 "Trade Server Return Codes")
RET_REQUOTE, RET_PLACED, RET_DONE, RET_DONE_PARTIAL = 10004, 10008, 10009, 10010
RET_PRICE_CHANGED, RET_PRICE_OFF, RET_TOO_MANY, RET_INVALID_FILL, RET_CONNECTION = 10020, 10021, 10024, 10030, 10031
OK_RETCODES = (RET_PLACED, RET_DONE, RET_DONE_PARTIAL)
# bei diesen Codes hat der Broker nichts ausgeführt -> erneuter Versuch ist gefahrlos
RETRY_SAFE = (RET_REQUOTE, RET_PRICE_CHANGED, RET_PRICE_OFF, RET_TOO_MANY, RET_CONNECTION)
# Bitfeld symbol_info.expiration_mode und Order-Zustände der Historie
EXP_GTC, EXP_DAY, EXP_SPECIFIED = 1, 2, 4
ORDER_STATE_CANCELED, ORDER_STATE_REJECTED, ORDER_STATE_EXPIRED = 2, 5, 6
MAX_CLOCK_SKEW = 60   # so viele Sekunden darf die PC-Uhr von der Broker-Uhr abweichen
# Ablehnungen, die man selbst beheben kann - im Log als Text statt nur als Zahl
RETCODE_TEXT = {
    10006: "vom Broker abgelehnt", 10013: "ungültige Anfrage", 10014: "ungültige Lotgröße",
    10015: "ungültiger Preis", 10016: "SL/TP ungültig oder zu nah am Kurs",
    10017: "Handel in diesem Symbol gesperrt", 10018: "Markt geschlossen",
    10019: "nicht genug freie Margin", 10022: "ungültige Ablaufzeit",
    10026: "Broker erlaubt auf diesem Konto keinen Automatik-Handel",
    10027: "Algo-Handel im MT5 aus oder für Python gesperrt (ANLEITUNG_MT5.md, Schritt 2)",
    10030: "Füllart vom Broker nicht unterstützt", 10031: "keine Verbindung zum Handelsserver",
    10033: "zu viele wartende Orders", 10040: "zu viele offene Positionen",
    10042: "nur Long erlaubt", 10043: "nur Short erlaubt", 10044: "nur Schließen erlaubt",
}


def failure_text(res, mt5) -> str:
    if res is None:
        return f"keine Antwort vom Terminal ({mt5.last_error()})"
    txt = RETCODE_TEXT.get(res.retcode)
    return f"Retcode {res.retcode}" + (f": {txt}" if txt else "")


def setup_logging():
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    for h in (logging.FileHandler(HERE / C.LOG_FILE, encoding="utf-8"), logging.StreamHandler()):
        h.setFormatter(fmt)
        log.addHandler(h)


def ny_ts(ts: float) -> pd.Timestamp:
    return pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(data.NY)


class ServerClock:
    """Umrechnung Broker-Serverzeit <-> UTC nach der Regel SERVER_TZ - für jeden Zeitpunkt
    einzeln, also auch über Sommer-/Winterzeit-Umstellungen hinweg korrekt.

    "NY+7"          Serverzeit = New-York-Zeit + 7 h (GMT+2/+3 mit US-Umstellung, sehr verbreitet)
    "UTC+2"         feste Verschiebung ohne Umstellung
    "Europe/Athens" beliebige IANA-Zeitzone (z.B. GMT+2/+3 mit EU-Umstellung)
    """

    def __init__(self, rule: str):
        self.rule = rule.strip()
        r = self.rule.upper()
        self.kind, self.hours, self.tz = "tz", 0.0, self.rule
        if r.startswith("NY"):
            self.kind, self.hours = "ny", float(r[2:] or 0)
        elif r.startswith("UTC") or r.startswith("GMT"):
            self.kind, self.hours = "fixed", float(r[3:] or 0)

    def offset_at(self, utc_ts: float) -> int:
        """Serverzeit minus UTC (Sekunden) zum Zeitpunkt utc_ts."""
        if self.kind == "fixed":
            return int(self.hours * 3600)
        ts = pd.Timestamp(utc_ts, unit="s", tz="UTC")
        if self.kind == "ny":
            return int(ts.tz_convert(data.NY).utcoffset().total_seconds() + self.hours * 3600)
        return int(ts.tz_convert(self.tz).utcoffset().total_seconds())

    def to_utc(self, server_secs) -> pd.DatetimeIndex:
        """Serverzeiten (Sekunden) -> UTC; nicht eindeutige Zeiten (Umstellungsstunde) -> NaT."""
        srv = pd.to_datetime(server_secs, unit="s")
        if self.kind == "fixed":
            return (srv - pd.Timedelta(hours=self.hours)).tz_localize("UTC")
        if self.kind == "ny":
            loc = (srv - pd.Timedelta(hours=self.hours)).tz_localize(data.NY, ambiguous="NaT", nonexistent="NaT")
        else:
            loc = srv.tz_localize(self.tz, ambiguous="NaT", nonexistent="NaT")
        return loc.tz_convert("UTC")


class SmcBot:
    def __init__(self, mt5, clock=time.time, cfg=None, workdir: Path = HERE):
        self.mt5 = mt5
        self.clock = clock
        self.cfg = cfg or json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.eng = self.cfg["engine"]
        self.flat_m = hhmm(self.eng.get("flat_at", "16:00"))
        self.early = {d: hhmm(v) for d, v in (self.eng.get("early_close") or {}).items()}
        self.srv = ServerClock(C.SERVER_TZ)
        self.workdir = Path(workdir)
        self.symbols: dict[str, str] = {}       # Farm-Name -> Broker-Symbol
        self.last_bar: dict[str, int] = {}      # Farm-Name -> zuletzt verarbeitete Kerze (UTC)
        self.held: dict[str, set] = {}          # Farm-Name -> armed_t, die die Engine noch hält
        self.dropped: dict[tuple, str] = {}     # (Farm-Name, armed_t) -> Grund, warum die Engine verwarf
        self.status: dict[str, str] = {}
        self.login = None
        self.clock_ok = None                    # None = noch nicht geprüft
        self.clock_state = None                 # "ok" | "tz" | "pc" - nur bei Wechsel ins Log
        self.skew = 0                           # Broker-Uhr minus PC-Uhr (Sekunden), siehe now()
        self.last_tick = None                   # neuester Tick der letzten Uhrprüfung
        self.account_ok = True
        self.trading_ok = True
        self.trade_block_msg = ""               # None = Handel erlaubt; "" = noch nicht geprüft
        self.known_positions: dict[int, dict] = {}
        self.pending_close: dict[int, list] = {}
        self.journal_backlog: list[dict] = []
        self.journal_warned = False
        self.last_heartbeat = 0.0
        self.last_flat_try = 0.0
        self.state = {"plans": {}, "day": None, "day_equity": None, "kill": False}
        self.load_state()

    # ------------------------------------------------------------------ Zustand / Journal
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
        try:
            tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as exc:
            log.error(f"Zustand konnte nicht gespeichert werden ({exc}).")

    def journal(self, row: dict):
        """Journal-Zeile schreiben. Ist die Datei gesperrt (z.B. in Excel geöffnet), wird die
        Zeile gemerkt und später nachgetragen - der Bot läuft weiter."""
        row.setdefault("zeit_ny", ny_ts(self.now()).strftime("%Y-%m-%d %H:%M:%S"))
        self.journal_backlog.append(row)
        p = self.path(C.JOURNAL_FILE)
        try:
            new = not p.exists()
            with open(p, "a", encoding="utf-8") as f:
                if new:
                    f.write(";".join(JOURNAL_COLS) + "\n")
                for r in self.journal_backlog:
                    f.write(";".join(str(r.get(k, "")) for k in JOURNAL_COLS) + "\n")
            self.journal_backlog.clear()
            self.journal_warned = False
        except OSError as exc:
            if not self.journal_warned:
                log.error(f"Journal nicht schreibbar ({exc}) - ist {C.JOURNAL_FILE} in Excel geöffnet? "
                          f"Die Zeilen werden nachgetragen, sobald die Datei frei ist.")
                self.journal_warned = True

    # ------------------------------------------------------------------ Verbindung / Konto
    def init_kwargs(self):
        kw = {"path": C.MT5_PATH} if getattr(C, "MT5_PATH", None) else {}
        if C.MT5_LOGIN:
            kw.update(login=C.MT5_LOGIN, password=C.MT5_PASSWORD, server=C.MT5_SERVER)
        return kw

    def connect(self):
        m = self.mt5
        if not m.initialize(**self.init_kwargs()):
            log.error(f"MT5-Initialisierung fehlgeschlagen: {m.last_error()}")
            log.error("Prüfen: MT5 gestartet und eingeloggt? Bot und MT5 beide normal gestartet (nicht eins "
                      "davon 'Als Administrator')? Mehrere MT5 installiert? Dann MT5_PATH in mt5_config.py setzen.")
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
        self.login = acc.login
        self.ensure_symbols()

    def ensure_connection(self) -> bool:
        """Terminal verbunden, gleiches (Demo-)Konto wie beim Start, Algo-Handel an?"""
        m = self.mt5
        if m.terminal_info() is None:
            log.warning("Verbindung zum MT5-Terminal verloren - versuche neu zu verbinden ...")
            m.shutdown()
            if not m.initialize(**self.init_kwargs()) or m.terminal_info() is None:
                return False
            log.info("Wieder mit dem MT5-Terminal verbunden.")
        acc = m.account_info()
        ok = (acc is not None and acc.login == self.login
              and (acc.trade_mode == m.ACCOUNT_TRADE_MODE_DEMO or C.ALLOW_REAL_ACCOUNT))
        if ok != self.account_ok:
            if ok:
                log.info(f"Wieder mit dem Startkonto {self.login} verbunden - Bot arbeitet weiter.")
            else:
                log.error(f"FALSCHES KONTO: Der Bot wurde mit Demokonto {self.login} gestartet, im Terminal ist "
                          f"jetzt {acc.login if acc else 'keins'} - der Bot handelt nicht, bis das "
                          f"Startkonto wieder eingeloggt ist.")
        self.account_ok = ok
        if not ok:
            return False
        block = self.trade_block(m.terminal_info(), acc)
        if block != self.trade_block_msg:
            if block is None:
                log.info("Handel über die MT5-Python-API ist erlaubt.")
            else:
                log.error(f"KEIN HANDEL: {block}. Der Bot beobachtet weiter, platziert aber keine neuen Orders.")
        self.trade_block_msg = block
        self.trading_ok = block is None
        return True

    @staticmethod
    def trade_block(term, acc):
        """None, wenn der Bot über die API handeln darf - sonst der Grund und was zu tun ist."""
        if term is None or acc is None:
            return "keine Verbindung zum MT5-Terminal"
        if not getattr(term, "trade_allowed", True):
            return "Algo-Handel ist im MT5-Terminal AUS - oben in der Leiste auf 'Algo Trading' klicken"
        if getattr(term, "tradeapi_disabled", False):
            return ("Handel über die Python-API ist im MT5 gesperrt - Extras -> Optionen -> Expert Advisors: "
                    "den Haken bei 'Algo-Handel über externe Python-API deaktivieren' entfernen")
        if not getattr(acc, "trade_allowed", True):
            return ("dieses Konto darf nicht handeln - meist mit dem Investor-Passwort (nur Lesen) eingeloggt; "
                    "mit dem normalen Passwort neu einloggen")
        if not getattr(acc, "trade_expert", True):
            return "der Broker erlaubt auf diesem Konto keinen Automatik-Handel"
        return None

    def find_symbol(self, key, sym):
        """Der eingetragene Name - sonst die EINE Broker-Variante desselben Tickers (NVDA.US, #NVDA, NVDAm).
        Gibt (Name oder None, Vorschläge) zurück; bei mehreren Varianten wird nichts geraten."""
        m = self.mt5
        if m.symbol_info(sym) is not None:
            return sym, []
        names = [s.name for s in (m.symbols_get(group=f"*{key}*") or [])]
        same = re.compile(rf"#?{re.escape(key)}(?:[._-][A-Za-z0-9]{{1,6}}|[a-z])?")
        hits = [n for n in names if same.fullmatch(n)]
        return (hits[0] if len(hits) == 1 else None), (hits or names)[:8]

    def ensure_symbols(self):
        m = self.mt5
        for key, wanted in C.MARKETS.items():
            sym, hints = self.find_symbol(key, wanted)
            info = m.symbol_info(sym) if sym else None
            if info is None:
                log.warning(f"{key}: Symbol '{wanted}' beim Broker nicht gefunden - übersprungen."
                            + (f" Mögliche Namen: {', '.join(hints)} - den richtigen in mt5_config.py "
                               f"unter MARKETS eintragen." if hints else ""))
                continue
            if sym != wanted:
                log.info(f"{key}: '{wanted}' gibt es beim Broker nicht - nehme '{sym}' (automatisch erkannt).")
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

    # ------------------------------------------------------------------ Zeit & Daten
    def now(self) -> float:
        """Jetzt (UTC-Sekunden) nach der Broker-Uhr: PC-Uhr plus gemessene Abweichung."""
        return self.clock() + self.skew

    def check_clock(self):
        """Passt die Serverzeit zur Regel SERVER_TZ, und geht die PC-Uhr richtig? Gemessen wird nur
        an einem Tick, der seit der letzten Prüfung neu ist - der ist höchstens Sekunden alt.
        Passt etwas nicht, werden keine neuen Orders platziert (lieber nichts tun als falsch)."""
        newest = None
        for sym in list(self.symbols.values()) + list(C.CLOCK_SYMBOLS):
            tick = self.mt5.symbol_info_tick(sym)
            if tick is not None and tick.time and (newest is None or tick.time > newest):
                newest = tick.time
        fresh = newest is not None and self.last_tick is not None and newest > self.last_tick
        if newest is not None:
            self.last_tick = newest
        if not fresh:
            return  # kein neuer Tick (Markt zu, Wochenende): alter Tick wäre keine sichere Messung
        now = self.clock()
        diff = newest - now
        measured = round(diff / 1800) * 1800
        expected = self.srv.offset_at(now)
        if measured != expected:
            state = "tz"
            # eine falsche Windows-Zeitzone sieht genauso aus wie eine falsche Regel -> PC zuerst prüfen
            msg = (f"Broker-Serverzeit ist UTC{measured / 3600:+.1f} h, nach SERVER_TZ = '{self.srv.rule}' "
                   f"wären es UTC{expected / 3600:+.1f} h - bis dahin keine neuen Orders. Zuerst prüfen: "
                   f"Ist es in New York gerade {ny_ts(now):%H:%M} Uhr? Wenn nicht, stimmt die Windows-Uhr "
                   f"oder ihre Zeitzone nicht. Wenn ja, SERVER_TZ in mt5_config.py anpassen.")
        else:
            skew = diff - expected
            self.skew = skew if abs(skew) > MAX_CLOCK_SKEW else 0
            state = "pc" if self.skew else "ok"
            msg = (f"Broker-Serverzeit geprüft: UTC{measured / 3600:+.1f} h passt zu SERVER_TZ = "
                   f"'{self.srv.rule}'." if not self.skew else
                   f"Die PC-Uhr geht {abs(skew):.0f} s {'vor' if skew < 0 else 'nach'}. Bitte die Windows-Uhr "
                   f"stellen (Einstellungen > Zeit und Sprache > Jetzt synchronisieren) - bis dahin keine "
                   f"neuen Orders. Glattgestellt wird nach der Uhr des Brokers.")
        if state != self.clock_state:
            (log.info if state == "ok" else log.error)(msg)
        self.clock_state, self.clock_ok = state, state == "ok"

    def get_bars(self, sym: str) -> pd.DataFrame | None:
        """Geschlossene 5m-Kerzen der regulären US-Handelszeit, Zeit in UTC-Sekunden.

        'Geschlossen' entscheidet die Uhr des Brokers: eine Kerze zählt erst, wenn für dieses
        Symbol ein Tick mit Zeitstempel ab ihrem Ende angekommen ist."""
        tick = self.mt5.symbol_info_tick(sym)
        if tick is None or not tick.time:
            return None
        rates = self.mt5.copy_rates_from_pos(sym, self.mt5.TIMEFRAME_M5, 0, C.BARS_HISTORY)
        if rates is None or len(rates) < 50:
            return None
        rates = rates[rates["time"].astype("int64") + 300 <= int(tick.time)]
        utc = self.srv.to_utc(rates["time"].astype("int64"))
        keep = ~utc.isna()
        t = ((utc[keep] - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)).astype("int64")
        df = pd.DataFrame({
            "t": t.to_numpy(),
            "o": rates["open"][keep].astype(float), "h": rates["high"][keep].astype(float),
            "l": rates["low"][keep].astype(float), "c": rates["close"][keep].astype(float),
            "v": rates["tick_volume"][keep].astype("int64"),
        })
        df = df[df.t + 300 <= self.now() + 120]      # Plausibilität gegen die Uhr
        df = data.add_session_cols(df)
        df = df[(df.mins >= data.SESSION_OPEN_MIN) & (df.mins < data.SESSION_CLOSE_MIN)]
        df = df.drop_duplicates("t").sort_values("t").reset_index(drop=True)
        return df if len(df) >= 50 else None

    def flat_min(self, day: str) -> int:
        """Minute (New York), zu der die Schluss-Kerze endet: flat_at, an halben Tagen 5 Min. vor Schluss."""
        return min(self.flat_m, self.early[day] - 5) if day in self.early else self.flat_m

    def close_min(self, day: str) -> int:
        return self.early.get(day, data.SESSION_CLOSE_MIN)

    def flat_utc(self, day: str) -> float:
        return (pd.Timestamp(day, tz=data.NY) + pd.Timedelta(minutes=self.flat_min(day))).timestamp()

    # ------------------------------------------------------------------ Broker-Helfer
    def my_positions(self, sym=None):
        """Positionen dieses Bots - oder None, wenn die Abfrage fehlschlägt (dann nichts tun)."""
        res = self.mt5.positions_get(symbol=sym) if sym else self.mt5.positions_get()
        return None if res is None else [p for p in res if p.magic == C.MAGIC_NUMBER]

    def my_orders(self, sym=None):
        res = self.mt5.orders_get(symbol=sym) if sym else self.mt5.orders_get()
        return None if res is None else [o for o in res if o.magic == C.MAGIC_NUMBER]

    def fillings(self, info, pending=False):
        m = self.mt5
        modes = [m.ORDER_FILLING_RETURN] if pending else []
        fm = int(getattr(info, "filling_mode", 0) or 0)
        if fm & 1:
            modes.append(m.ORDER_FILLING_FOK)
        if fm & 2:
            modes.append(m.ORDER_FILLING_IOC)
        if not pending:
            modes.append(m.ORDER_FILLING_RETURN)
        return list(dict.fromkeys(modes))

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
        Grund, warum nicht gehandelt wird. Reicht die Margin nicht für das volle Risiko,
        wird die Größe verkleinert.
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
        info = m.symbol_info(pos.symbol)
        is_buy = pos.type == m.POSITION_TYPE_BUY
        res = None
        for _ in range(3):  # bei Requote/Preisänderung sofort mit frischem Kurs neu
            tick = m.symbol_info_tick(pos.symbol)
            if info is None or tick is None:
                break
            req = {"action": m.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume,
                   "type": m.ORDER_TYPE_SELL if is_buy else m.ORDER_TYPE_BUY, "position": pos.ticket,
                   "price": tick.bid if is_buy else tick.ask, "deviation": 100, "magic": C.MAGIC_NUMBER,
                   "comment": f"ADF {why}"[:31], "type_time": m.ORDER_TIME_GTC}
            res = self.send(req, self.fillings(info))
            if res is not None and res.retcode in (RET_DONE, RET_DONE_PARTIAL):
                log.info(f"{pos.symbol}: Position {pos.ticket} geschlossen ({why}).")
                return True
            if res is None or res.retcode not in RETRY_SAFE:
                break
        log.error(f"{pos.symbol}: Schließen von {pos.ticket} FEHLGESCHLAGEN ({failure_text(res, m)}) "
                  f"- neuer Versuch folgt.")
        return False

    def cancel_order(self, order, why: str) -> bool:
        m = self.mt5
        res = m.order_send({"action": m.TRADE_ACTION_REMOVE, "order": order.ticket})
        ok = res is not None and res.retcode in (RET_DONE, RET_PLACED)
        plan = self.state["plans"].get(str(order.ticket), {})
        if ok:
            log.info(f"{order.symbol}: Limit-Order {order.ticket} gelöscht ({why}).")
            plan["status"] = "storniert"
            self.save_state()
            self.journal({"ereignis": "STORNO", "markt": plan.get("key", ""), "symbol": order.symbol,
                          "richtung": plan.get("dir", ""), "entry": plan.get("entry", ""), "grund": why})
        else:
            log.error(f"{order.symbol}: Löschen von Order {order.ticket} fehlgeschlagen "
                      f"({failure_text(res, m)}) - neuer Versuch folgt.")
        return ok

    # ------------------------------------------------------------------ Wächter
    def daily_guard(self):
        acc = self.mt5.account_info()
        if acc is None:
            return
        today = ny_ts(self.now()).strftime("%Y-%m-%d")
        if self.state["day"] != today:
            self.state.update(day=today, day_equity=acc.equity, kill=False)
            self.save_state()
        limit = self.state["day_equity"] * (1 - C.DAILY_LOSS_LIMIT_PERCENT / 100)
        if C.DAILY_LOSS_LIMIT_PERCENT > 0 and not self.state["kill"] and acc.equity <= limit:
            self.state["kill"] = True
            self.save_state()
            log.error(f"TAGESVERLUST-GRENZE erreicht (Kapital {acc.equity:.2f} <= {limit:.2f}). "
                      f"Heute keine neuen Orders mehr; wartende Limit-Orders werden gelöscht.")
            for o in self.my_orders() or []:
                self.cancel_order(o, "Tagesverlust-Grenze")

    def flat_guard(self):
        """Außerhalb von 09:30 bis zur Schluss-Kerze hält der Bot nichts - unabhängig davon, ob
        noch Kerzen kommen. Bis zum Börsenschluss wird bei jedem Durchlauf neu versucht,
        danach alle 5 Minuten."""
        nyt = ny_ts(self.now())
        day, mins = nyt.strftime("%Y-%m-%d"), nyt.hour * 60 + nyt.minute
        weekday = nyt.weekday() < 5
        if weekday and data.SESSION_OPEN_MIN <= mins < self.flat_min(day):
            return
        in_window = weekday and self.flat_min(day) <= mins < self.close_min(day)
        if not in_window and self.now() - self.last_flat_try < 300:
            return
        pos, ords = self.my_positions(), self.my_orders()
        if pos is None or ords is None or (not pos and not ords):
            return
        self.last_flat_try = self.now()
        for o in ords:
            self.cancel_order(o, "Sessionende")
        for p in pos:
            self.close_position(p, "Schluss")

    def guard_protection(self):
        """Jede Position MUSS SL und TP beim Broker haben."""
        m = self.mt5
        for pos in self.my_positions() or []:
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

    # ------------------------------------------------------------------ Einstiege, Ausstiege, Journal
    def journal_open(self, plan, symbol, volume, price, sl, tp):
        log.info(f"⭐ EINSTIEG: {str(plan.get('dir', '')).upper()} {symbol} {volume} @ {price} | SL {sl} | TP {tp}")
        self.journal({"ereignis": "OPEN", "markt": plan.get("key"), "symbol": symbol, "richtung": plan.get("dir"),
                      "lots": volume, "entry": price, "sl": sl, "tp": tp,
                      "risiko": f"{plan.get('risk_money', 0):.2f}", "setup": plan.get("chain", ""),
                      "kapital": self.equity_txt()})

    def track_positions(self):
        current = self.my_positions()
        if current is None:
            return  # Abfrage fehlgeschlagen: NICHT als "alles geschlossen" deuten
        current = {p.ticket: p for p in current}
        for ticket, pos in current.items():
            if ticket in self.known_positions:
                continue
            pid = str(pos.identifier)
            self.known_positions[ticket] = {"symbol": pos.symbol, "plan_id": pid, "pos_id": pos.identifier}
            plan = self.state["plans"].get(pid)
            if plan and plan.get("status") != "gefüllt":
                plan["status"] = "gefüllt"
                self.save_state()
                self.journal_open(plan, pos.symbol, pos.volume, pos.price_open, pos.sl, pos.tp)
        for ticket in [t for t in self.known_positions if t not in current]:
            info = self.known_positions.pop(ticket)
            if all(e[0]["plan_id"] != info["plan_id"] for e in self.pending_close.values()):
                self.pending_close[ticket] = [info, 0]   # (reconcile_plans kann sie schon eingereiht haben)

    def process_pending_closes(self):
        """Die Broker-Historie ist manchmal erst Sekunden später da -> bis zu 10 Versuche."""
        for ticket, entry in list(self.pending_close.items()):
            entry[1] += 1
            if self.journal_close(entry[0]):
                del self.pending_close[ticket]
            elif entry[1] >= 10:
                del self.pending_close[ticket]
                log.warning(f"{entry[0]['symbol']}: keine Abschlussdaten für Position {entry[0]['pos_id']} "
                            f"in der Historie - CLOSE-Zeile fehlt im Journal.")
                plan = self.state["plans"].get(entry[0]["plan_id"])
                if plan and plan.get("status") == "gefüllt":
                    plan["status"] = "unbekannt"
                    self.save_state()

    def reconcile_plans(self):
        """Orders und Positionen, die beim Broker verschwunden sind, ohne dass der Bot es gesehen hat
        (zwischen zwei Durchläufen oder während der Bot aus war): gefüllt, geschlossen, verfallen?"""
        m = self.mt5
        orders, positions = self.my_orders(), self.my_positions()
        if orders is None or positions is None:
            return
        live = {o.ticket for o in orders} | {p.identifier for p in positions}
        waiting = {e[0]["plan_id"] for e in self.pending_close.values()}
        changed = False
        for tk, plan in self.state["plans"].items():
            if int(tk) in live:
                continue
            if plan.get("status") == "gefüllt" and tk not in waiting:
                # Position wurde geschlossen, während der Bot aus war -> CLOSE-Zeile nachtragen
                self.pending_close[int(tk)] = [{"symbol": plan.get("symbol"), "plan_id": tk, "pos_id": int(tk)}, 0]
                continue
            if plan.get("status") != "wartet":
                continue
            deals = m.history_deals_get(position=int(tk)) or ()
            ins = [d for d in deals if d.entry == m.DEAL_ENTRY_IN]
            if ins:
                plan["status"], changed = "gefüllt", True
                self.journal_open(plan, plan.get("symbol"), ins[0].volume, ins[0].price, plan.get("sl"), plan.get("tp"))
                self.pending_close[int(tk)] = [{"symbol": plan.get("symbol"), "plan_id": tk, "pos_id": int(tk)}, 0]
                continue
            hist = m.history_orders_get(ticket=int(tk)) if hasattr(m, "history_orders_get") else None
            state = getattr(hist[0], "state", None) if hist else None
            if state in (ORDER_STATE_CANCELED, ORDER_STATE_REJECTED, ORDER_STATE_EXPIRED):
                plan["status"], changed = "storniert", True
                self.journal({"ereignis": "ABGELAUFEN", "markt": plan.get("key"), "symbol": plan.get("symbol"),
                              "richtung": plan.get("dir"), "entry": plan.get("entry"),
                              "grund": "beim Broker verfallen/gelöscht"})
            else:
                plan["tries"] = plan.get("tries", 0) + 1
                if plan["tries"] >= 10:
                    plan["status"] = "unbekannt"
                changed = True
        if changed:
            self.save_state()

    def journal_close(self, info) -> bool:
        m = self.mt5
        deals = m.history_deals_get(position=info["pos_id"]) or ()
        outs = [d for d in deals if d.entry in (m.DEAL_ENTRY_OUT, m.DEAL_ENTRY_OUT_BY)]
        if not outs:
            return False
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
        if plan:
            plan["status"] = "geschlossen"
            self.save_state()
        return True

    # ------------------------------------------------------------------ Strategie pro Aktie
    def cancel_dropped(self, key, orders):
        """Limit-Orders löschen, deren Setup die Engine nicht mehr hält (oder die zu alt sind)."""
        max_age = (self.eng["entry_window"] + 2) * 300
        held = self.held.get(key, set())
        for o in orders:
            plan = self.state["plans"].get(str(o.ticket), {})
            armed = plan.get("armed_t")
            too_old = self.now() - plan.get("placed_utc", self.now()) > max_age
            if armed not in held or too_old:
                why = "zu alt" if armed in held else self.dropped.get((key, armed), "Setup nicht mehr gültig")
                self.cancel_order(o, why)

    def process_symbol(self, key: str, sym: str):
        df = self.get_bars(sym)
        if df is None:
            return
        newest = int(df.t.iloc[-1])
        prev = self.last_bar.get(key)
        live_pos, live_ord = self.my_positions(sym), self.my_orders(sym)
        if live_pos is None or live_ord is None:
            return  # Abfrage fehlgeschlagen -> im nächsten Durchlauf erneut
        if prev is not None and newest <= prev:      # keine neue Kerze (oder kurz unvollständige Daten)
            if key in self.held and live_ord:
                self.cancel_dropped(key, live_ord)   # fehlgeschlagene Löschungen sofort wiederholen
            return
        self.last_bar[key] = newest
        first = prev is None
        day = df.day.iloc[-1]
        if int(df.mins.iloc[-1]) + 5 >= self.flat_min(day):
            for o in live_ord:
                self.cancel_order(o, "Sessionende")
            for p in live_pos:
                self.close_position(p, "Schluss")
            self.held[key] = set()
            self.status[key] = "Schluss - glattgestellt"
            return

        res = analyze(df, key, self.cfg, open_end=True)
        st = res["state"]
        eng_ord = st.get("order")
        # Setups, die die Engine noch hält: wartende Order oder (simulierte) offene Position
        self.held[key] = {x["armed_t"] for x in (st.get("order"), st.get("pos")) if x}
        for r in res["refusals"]:
            if r.get("armed_t"):
                self.dropped[(key, r["armed_t"])] = r["reason"]
        self.status[key] = ("im Trade" if live_pos else "Limit aktiv" if live_ord else
                            "Sweep - warte auf MSS" if st.get("cand") else "beobachte")
        self.cancel_dropped(key, live_ord)
        if first or eng_ord is None or eng_ord["armed_t"] != newest:
            return
        if any(p.get("armed_t") == newest and p.get("key") == key for p in self.state["plans"].values()):
            return  # dieses Setup wurde schon platziert
        self.try_place(key, sym, eng_ord, day)

    def try_place(self, key, sym, o, day):
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
            return skip(f"kein Handel erlaubt: {self.trade_block_msg}")
        if self.clock_ok is not True:
            return skip("Broker-Serverzeit nicht bestätigt (SERVER_TZ prüfen)")
        if self.state.get("kill"):
            return skip("Tagesverlust-Grenze erreicht")
        pos_s, ord_s, pos_all, ord_all = self.my_positions(sym), self.my_orders(sym), self.my_positions(), self.my_orders()
        if any(x is None for x in (pos_s, ord_s, pos_all, ord_all)):
            return skip("Positionsabfrage beim Broker fehlgeschlagen")
        if pos_s or ord_s:
            return skip("schon Position/Order in dieser Aktie")
        if len(pos_all) + len(ord_all) >= C.MAX_OPEN_POSITIONS:
            return skip(f"max. {C.MAX_OPEN_POSITIONS} Positionen/Orders erreicht")
        info, tick = m.symbol_info(sym), m.symbol_info_tick(sym)
        if info is None or tick is None or not tick.bid or not tick.ask:
            return skip("keine aktuellen Kurse")
        tm = getattr(info, "trade_mode", 4)  # 0 aus, 1 nur Long, 2 nur Short, 3 nur Schließen, 4 voll
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

        # Ablauf beim Broker: so lange, wie die Engine auf den Retest wartet, höchstens bis zum Schluss
        end_utc = min(o["armed_t"] + (self.eng["entry_window"] + 1) * 300, self.flat_utc(day))
        if not int(getattr(info, "expiration_mode", 0) or 0) & EXP_SPECIFIED:
            # ohne feste Ablaufzeit könnte die Order nach einem Absturz noch nach Schluss gefüllt werden
            return skip("Broker erlaubt keine Ablaufzeit für Limit-Orders (expiration_mode)")
        timing = {"type_time": m.ORDER_TIME_SPECIFIED, "expiration": int(end_utc + self.srv.offset_at(end_utc))}

        sized = self.calc_lots(sym, info, long, entry, sl)
        if isinstance(sized, str):
            return skip(sized)
        lots, risk_money = sized

        req = {"action": m.TRADE_ACTION_PENDING, "symbol": sym, "volume": lots,
               "type": m.ORDER_TYPE_BUY_LIMIT if long else m.ORDER_TYPE_SELL_LIMIT,
               "price": entry, "sl": sl, "tp": tp, "magic": C.MAGIC_NUMBER,
               "comment": f"ADF {o['armed_t']}"[:31], **timing}
        res = self.send(req, self.fillings(info, pending=True))
        if res is None or res.retcode not in OK_RETCODES:
            return skip(f"Order abgelehnt ({failure_text(res, m)})")
        self.state["plans"][str(res.order)] = {
            "key": key, "symbol": sym, "dir": o["dir"], "armed_t": o["armed_t"], "entry": entry,
            "sl": sl, "tp": tp, "lots": lots, "risk_money": risk_money, "chain": chain,
            "placed_utc": self.now(), "status": "wartet"}
        self.prune_plans()
        self.save_state()
        log.info(f"★ {head}: Limit {lots} @ {entry} | SL {sl} | TP {tp} | Risiko {risk_money:.2f} | "
                 f"verfällt {ny_ts(end_utc):%H:%M} NY")
        self.journal({"ereignis": "ORDER", "markt": key, "symbol": sym, "richtung": o["dir"], "lots": lots,
                      "entry": entry, "sl": sl, "tp": tp, "risiko": f"{risk_money:.2f}", "setup": chain,
                      "kapital": self.equity_txt()})

    def prune_plans(self, keep_days=14):
        cutoff = self.now() - keep_days * 86400
        plans = self.state["plans"]
        for k in [k for k, v in plans.items() if v.get("status") in ("geschlossen", "storniert", "unbekannt")
                  and v.get("placed_utc", 0) < cutoff]:
            del plans[k]

    # ------------------------------------------------------------------ Schleife
    def equity_txt(self):
        acc = self.mt5.account_info()
        return f"{acc.equity:.2f}" if acc else ""

    def heartbeat(self, force=False):
        if not force and self.now() - self.last_heartbeat < C.HEARTBEAT_MINUTES * 60:
            return
        self.last_heartbeat = self.now()
        acc = self.mt5.account_info()
        eq = f"Kapital {acc.equity:.2f} {acc.currency}" if acc else "Kapital ?"
        log.info(f"♥ {ny_ts(self.now()):%H:%M} New York - {eq}, {len(self.my_positions() or [])} Position(en), "
                 f"{len(self.my_orders() or [])} Limit-Order(s){' - TAGESGRENZE AKTIV' if self.state.get('kill') else ''}"
                 f"{'' if self.clock_ok else ' - SERVERZEIT NICHT BESTÄTIGT'}"
                 f"{'' if self.trading_ok else ' - HANDEL GESPERRT (siehe Fehler oben)'}")
        for key in self.symbols:
            log.info(f"   {key:<6} {self.status.get(key, 'warte auf Daten / Markt geschlossen')}")

    def step(self):
        if not self.ensure_connection():
            return
        # jeder Teil einzeln abgesichert: ein Fehler darf Schutz und Glattstellen nicht verhindern
        for part in (self.check_clock, self.track_positions, self.process_pending_closes, self.reconcile_plans,
                     self.guard_protection, self.daily_guard, self.flat_guard):
            try:
                part()
            except Exception:
                log.exception(f"Fehler in {part.__name__}")
        for key, sym in self.symbols.items():
            try:
                self.process_symbol(key, sym)
            except Exception:
                log.exception(f"{key}: Fehler bei der Verarbeitung")
        try:
            self.heartbeat()
        except Exception:
            log.exception("Fehler im Herzschlag")

    def acquire_lock(self):
        """Nur eine Instanz pro Ordner: zwei Bots würden sich gegenseitig Orders löschen."""
        self._lock_fd = os.open(self.path("bot_smc.lock"), os.O_RDWR | os.O_CREAT)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock_fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.error("Der Bot läuft bereits (zweites Fenster offen?) - Abbruch.")
            sys.exit(1)

    def run(self):
        log.info("=" * 64)
        log.info("Aktien Data Farm - MT5-Autopilot (SMC-Engine) startet")
        log.info(f"Strategie aus config.json: RR {self.eng['target_rr']}, Einstiege "
                 f"{self.eng['no_entry_before']}-{self.eng['no_entry_after']} NY, glatt um {self.eng.get('flat_at')} NY, "
                 f"Risiko {C.RISK_PERCENT}%/Trade, Tagesgrenze -{C.DAILY_LOSS_LIMIT_PERCENT}%, "
                 f"Serverzeit-Regel '{C.SERVER_TZ}'")
        self.acquire_lock()
        self.connect()
        # offene Positionen übernimmt track_positions im ersten Durchlauf - so bekommt auch eine
        # Order, die gefüllt wurde, während der Bot aus war, ihre OPEN-Zeile
        log.info("Warte auf neue Kerzen ... (Stopp: Strg + C - SL/TP bleiben beim Broker aktiv)")
        try:
            while True:
                try:
                    self.step()
                except Exception:
                    log.exception("Unerwarteter Fehler - der Bot läuft weiter")
                time.sleep(C.POLL_SECONDS)
        except KeyboardInterrupt:
            for o in self.my_orders() or []:  # ohne Bot würde niemand sie rechtzeitig löschen
                self.cancel_order(o, "Bot gestoppt")
            log.info("Bot gestoppt. Wartende Limit-Orders wurden gelöscht. Offene Positionen sind durch "
                     "SL/TP beim Broker gesichert - aber ohne Bot werden sie NICHT zum Schluss glattgestellt!")
        finally:
            self.mt5.shutdown()


def disable_quickedit():
    """Windows: ein Klick ins Konsolenfenster (QuickEdit) würde den Bot anhalten."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)
        mode = ctypes.c_uint()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)
    except Exception:
        pass


def main():
    setup_logging()
    disable_quickedit()
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("FEHLER: MetaTrader5-Paket fehlt. Installieren mit:  pip install MetaTrader5")
        sys.exit(1)
    SmcBot(mt5).run()


if __name__ == "__main__":
    main()

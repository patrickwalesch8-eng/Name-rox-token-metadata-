"""Live MT5 bot against a simulated broker (tests/fake_mt5.py).

Run with:  python -m unittest discover -s tests   (from the trading-farm folder)
"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mt5"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mt5_config as C  # noqa: E402
from bot_smc import SmcBot, RET_DONE_PARTIAL  # noqa: E402
from fake_mt5 import FakeMT5  # noqa: E402
from farmlib import data  # noqa: E402
from farmlib.pipeline import analyze  # noqa: E402

BASE_CFG = json.loads((ROOT / "config.json").read_text())
# looser filters only for these tests, so the short demo replay contains several setups -
# the tests check the bot's mechanics, not the strategy
CFG = copy.deepcopy(BASE_CFG)
CFG["engine"].update(use_htf_bias=False, mss_window=30, entry_window=24, max_risk_atr=6.0)
DAYS, HISTORY_DAYS = 10, 5
PATCHED = ("MARKETS", "CLOCK_SYMBOLS", "BARS_HISTORY", "SERVER_TZ", "JOURNAL_FILE")


def patch_config(**kw):
    saved = {k: getattr(C, k) for k in PATCHED}
    C.MARKETS, C.CLOCK_SYMBOLS, C.BARS_HISTORY, C.SERVER_TZ = {"TST": "TST.US"}, [], 5000, "NY+7"
    for k, v in kw.items():
        setattr(C, k, v)
    return saved


def restore_config(saved):
    for k, v in saved.items():
        setattr(C, k, v)


def find_busy_series():
    """A deterministic demo series whose replay window contains several setups."""
    for seed in range(100):
        df = data.make_demo("TST", days=DAYS, seed=seed, end="2026-10-16")
        res = analyze(data.add_session_cols(df), "TST", CFG)
        start = df.t.iloc[HISTORY_DAYS * 78]
        armed = {x["armed_t"] for x in res["trades"] + res["refusals"] if x.get("armed_t") and x["armed_t"] > start}
        if len(armed) >= 2:
            return df, armed
    raise RuntimeError("no demo series with setups")


class LiveBot(unittest.TestCase):
    """Replays five trading days bar by bar: the bot polls a few seconds after every close."""

    @classmethod
    def setUpClass(cls):
        cls.df, cls.expected = find_busy_series()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.saved = patch_config()
        cls.fake = FakeMT5({"TST.US": cls.df})
        cls.bot = SmcBot(cls.fake, clock=lambda: cls.fake.now, cfg=CFG, workdir=Path(cls.tmp.name))
        cls.fake.advance_to(int(cls.df.t.iloc[0]))
        cls.bot.connect()
        cls.unprotected, cls.held_overnight = [], []
        for i in range(HISTORY_DAYS * 78, len(cls.df)):
            close_t = int(cls.df.t.iloc[i]) + 300
            cls.fake.advance_to(close_t + 5)
            cls.bot.step()
            cls.unprotected += [p.ticket for p in cls.fake.positions.values() if not (p.sl and p.tp)]
            if i + 1 == len(cls.df) or cls.df.t.iloc[i + 1] - cls.df.t.iloc[i] > 3600:   # end of a session
                cls.fake.advance_to(close_t + 360)
                cls.bot.step()
                if cls.fake.positions or cls.fake.orders:
                    cls.held_overnight.append(close_t)

    @classmethod
    def tearDownClass(cls):
        restore_config(cls.saved)
        cls.tmp.cleanup()

    def journal(self):
        return (Path(self.tmp.name) / C.JOURNAL_FILE).read_text(encoding="utf-8")

    def test_server_clock_confirmed(self):
        self.assertIs(self.bot.clock_ok, True)

    def test_orders_match_the_engine(self):
        placed = {p["armed_t"] for p in self.bot.state["plans"].values()}
        self.assertTrue(placed, "the bot never placed an order")
        self.assertTrue(placed <= self.expected, f"orders without engine setup: {placed - self.expected}")
        # every engine setup was placed unless a live guard said no (journal SKIP)
        self.assertGreaterEqual(len(placed) + self.journal().count(";SKIP;"), len(self.expected))

    def test_every_order_has_stop_target_and_expiry(self):
        pending = [r for r in self.fake.sent if r["action"] == FakeMT5.TRADE_ACTION_PENDING]
        self.assertTrue(pending)
        plans = {p["armed_t"]: p for p in self.bot.state["plans"].values()}
        for req in pending:
            self.assertTrue(req["sl"] > 0 and req["tp"] > 0)
            if req["type"] == FakeMT5.ORDER_TYPE_BUY_LIMIT:
                self.assertTrue(req["sl"] < req["price"] < req["tp"])
            else:
                self.assertTrue(req["tp"] < req["price"] < req["sl"])
            self.assertEqual(req["type_time"], FakeMT5.ORDER_TIME_SPECIFIED)
            armed = int(req["comment"].split()[1])
            self.assertIn(armed, plans)
            day = pd.Timestamp(armed, unit="s", tz="UTC").tz_convert("America/New_York").strftime("%Y-%m-%d")
            end = min(armed + (CFG["engine"]["entry_window"] + 1) * 300, self.bot.flat_utc(day))  # never past 15:55
            self.assertEqual(req["expiration"], self.fake.srv(end))
        self.assertEqual(self.unprotected, [])

    def test_nothing_held_overnight(self):
        self.assertEqual(self.held_overnight, [])

    def test_one_position_or_order_per_symbol(self):
        self.assertLessEqual(len(self.fake.positions) + len(self.fake.orders), 1)

    def test_risk_is_about_one_percent(self):
        for p in self.bot.state["plans"].values():
            self.assertLessEqual(p["risk_money"], 10000 * 1.5 * C.RISK_PERCENT / 100 * 1.2)
            self.assertGreater(p["risk_money"], 0)

    def test_journal_written(self):
        self.assertIn(";ORDER;", self.journal())


class Safety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = patch_config()

    def tearDown(self):
        restore_config(self.saved)
        self.tmp.cleanup()

    def bot(self, df=None, at=100, **kw):
        df = df if df is not None else data.make_demo("TST", days=3, seed=1, end="2026-10-16")
        fake = FakeMT5({"TST.US": df}, **kw)
        fake.advance_to(int(df.t.iloc[at]) + 305)
        return fake, SmcBot(fake, clock=lambda: fake.now, cfg=CFG, workdir=Path(self.tmp.name))

    def confirm_clock(self, fake, bot, seconds=10):
        """The clock is only measured on a tick that is new since the previous check."""
        bot.check_clock()
        fake.advance_to(fake.now + seconds)
        bot.check_clock()

    def plan_for_try(self):
        return {"dir": "long", "entry": 1, "sl": 0.9, "tp": 1.2, "armed_t": 1, "sweep_lb": "PDL", "sweep_p": 1,
                "mss_k": "mss", "mss_p": 1, "zone": {"type": "FVG", "bot": 1, "top": 1}}

    def test_refuses_real_account(self):
        fake, bot = self.bot(demo=False)
        with self.assertRaises(SystemExit):
            bot.connect()

    def test_account_switch_stops_trading(self):
        fake, bot = self.bot()
        bot.connect()
        self.assertTrue(bot.ensure_connection())
        fake.login = 999                       # someone logs another account into the terminal
        self.assertFalse(bot.ensure_connection())
        self.assertFalse(bot.ensure_connection())   # stays blocked, not just for one poll
        fake.login = 123
        self.assertTrue(bot.ensure_connection())

    def test_no_retry_after_partial_fill(self):
        fake, bot = self.bot()
        calls = []
        fake.order_send = lambda req: calls.append(req) or SimpleNamespace(retcode=RET_DONE_PARTIAL)
        bot.send({"action": 1}, [0, 1, 2])
        self.assertEqual(len(calls), 1)

    def test_daily_loss_limit_blocks_new_orders(self):
        fake, bot = self.bot()
        bot.connect()
        self.confirm_clock(fake, bot)
        bot.daily_guard()
        fake.balance *= 1 - (C.DAILY_LOSS_LIMIT_PERCENT + 0.5) / 100
        bot.daily_guard()
        self.assertTrue(bot.state["kill"])
        n = len(fake.sent)
        bot.try_place("TST", "TST.US", self.plan_for_try(), "2026-10-16")
        self.assertEqual(len(fake.sent), n)

    def test_wrong_server_tz_blocks_orders(self):
        C.SERVER_TZ = "UTC+2"                  # broker really runs NY+7 (= UTC+3 in October)
        fake, bot = self.bot()
        bot.connect()
        self.confirm_clock(fake, bot)
        self.assertIs(bot.clock_ok, False)
        n = len(fake.sent)
        bot.try_place("TST", "TST.US", self.plan_for_try(), "2026-10-16")
        self.assertEqual(len(fake.sent), n)

    def test_bars_correct_across_dst_switch(self):
        df = data.make_demo("TST", days=10, seed=2, end="2026-11-06")    # US DST ends 2026-11-01
        fake, bot = self.bot(df=df, at=len(df) - 1)
        bot.connect()
        bars = bot.get_bars("TST.US")
        per_day = bars.groupby("day").mins.agg(["count", "min", "max"])
        self.assertEqual(len(per_day), 10)
        self.assertTrue((per_day["count"] == 78).all(), per_day)
        self.assertTrue((per_day["min"] == 570).all() and (per_day["max"] == 955).all(), per_day)
        self.assertEqual(sorted(bars.t.tolist()), sorted(df.t.tolist()))

    def test_unprotected_position_is_closed(self):
        fake, bot = self.bot()
        bot.connect()
        fake.positions[1] = SimpleNamespace(ticket=1, identifier=1, symbol="TST.US", magic=C.MAGIC_NUMBER, volume=1.0,
                                            type=0, price_open=100.0, sl=0.0, tp=0.0, comment="", time=0)
        bot.guard_protection()
        self.assertNotIn(1, fake.positions)   # no plan known -> closed

    def test_failed_position_query_is_not_read_as_closed(self):
        fake, bot = self.bot()
        bot.connect()
        bot.known_positions = {7: {"symbol": "TST.US", "plan_id": "7", "pos_id": 7}}
        fake.positions_get = lambda **kw: None
        bot.track_positions()
        self.assertEqual(bot.pending_close, {})
        self.assertIn(7, bot.known_positions)

    def test_locked_journal_does_not_stop_the_bot(self):
        fake, bot = self.bot()
        (Path(self.tmp.name) / "locked.csv").mkdir()   # opening a directory fails like an Excel lock
        C.JOURNAL_FILE = "locked.csv"
        bot.connect()
        bot.journal({"ereignis": "TEST"})
        bot.step()                                      # must not raise
        self.assertEqual(len(bot.journal_backlog), 1)  # kept for later

    def test_early_close_day_is_flattened_at_12_55(self):
        df = data.make_demo("TST", days=4, seed=3, end="2026-11-27", early_close={"2026-11-27": "13:00"})
        last = len(df) - 1                              # 12:55 bar of the half day
        fake, bot = self.bot(df=df, at=last - 1)        # now = 12:55:05 NY
        bot.connect()
        fake.positions[5] = SimpleNamespace(ticket=5, identifier=5, symbol="TST.US", magic=C.MAGIC_NUMBER, volume=1.0,
                                            type=0, price_open=100.0, sl=1.0, tp=500.0, comment="", time=0)
        self.assertEqual(ny_minutes(fake.now), 12 * 60 + 55)
        bot.flat_guard()
        self.assertNotIn(5, fake.positions)


    def test_stale_tick_does_not_flip_the_clock(self):
        fake, bot = self.bot()
        bot.connect()
        self.confirm_clock(fake, bot)
        self.assertIs(bot.clock_ok, True)
        frozen = fake.symbol_info_tick("TST.US")
        fake.symbol_info_tick = lambda s: frozen            # market closed: no new ticks
        for minutes in (25, 30, 60, 90):                     # old ticks look like a wrong time zone
            fake.now = frozen.time - fake.offset(fake.now) + minutes * 60
            bot.check_clock()
            self.assertIs(bot.clock_ok, True, minutes)

    def test_slow_pc_clock_blocks_orders_but_flattening_follows_the_broker(self):
        df = data.make_demo("TST", days=3, seed=1, end="2026-10-16")
        fake = FakeMT5({"TST.US": df})
        fake.advance_to(int(df.t.iloc[75]) + 305)                           # 15:50:05 New York
        bot = SmcBot(fake, clock=lambda: fake.now - 480, cfg=CFG, workdir=Path(self.tmp.name))   # PC 8 min slow
        bot.connect()
        self.confirm_clock(fake, bot)
        self.assertIs(bot.clock_ok, False)
        self.assertAlmostEqual(bot.now(), fake.now, delta=15)
        n = len(fake.sent)
        bot.try_place("TST", "TST.US", self.plan_for_try(), "2026-10-14")
        self.assertEqual(len(fake.sent), n)
        fake.positions[5] = SimpleNamespace(ticket=5, identifier=5, symbol="TST.US", magic=C.MAGIC_NUMBER, volume=1.0,
                                            type=0, price_open=100.0, sl=1.0, tp=500.0, comment="", time=0)
        fake.advance_to(int(df.t.iloc[76]) + 305)                           # 15:55:05 NY, PC shows 15:47
        bot.flat_guard()
        self.assertNotIn(5, fake.positions)

    def test_no_order_without_broker_expiry(self):
        fake, bot = self.bot(expiration_mode=2)                             # DAY only
        bot.connect()
        self.confirm_clock(fake, bot)
        self.assertIs(bot.clock_ok, True)
        n = len(fake.sent)
        bot.try_place("TST", "TST.US", self.plan_for_try(), "2026-10-14")
        self.assertEqual(len(fake.sent), n)
        self.assertIn("Ablaufzeit", (Path(self.tmp.name) / C.JOURNAL_FILE).read_text(encoding="utf-8"))

    def test_bar_going_backwards_keeps_the_order(self):
        fake, bot = self.bot()
        bot.connect()
        fake.orders[77] = SimpleNamespace(ticket=77, symbol="TST.US", magic=C.MAGIC_NUMBER, type=2, price_open=1.0,
                                          sl=0.9, tp=1.2, volume_current=1.0, comment="", time_setup=0,
                                          type_time=2, expiration=fake.srv(fake.now + 3600))
        bot.state["plans"]["77"] = {"key": "TST", "armed_t": 123, "placed_utc": fake.now, "status": "wartet"}
        bot.held["TST"] = {123}
        bot.last_bar["TST"] = int(fake.now) + 600    # a later bar was seen already; the data lags for one poll
        bot.process_symbol("TST", "TST.US")
        self.assertIn(77, fake.orders)

    def test_trades_while_the_bot_was_stopped_are_journaled(self):
        fake, bot = self.bot()
        # 42: filled before the stop, closed by its stop-loss while the bot was off
        bot.state["plans"]["42"] = {"key": "TST", "symbol": "TST.US", "dir": "long", "risk_money": 10.0,
                                    "status": "gefüllt", "placed_utc": fake.now}
        fake._deal(42, "TST.US", C.MAGIC_NUMBER, fake.DEAL_ENTRY_IN, 100.0, 1.0, 0.0, fake.DEAL_REASON_EXPERT, fake.now)
        fake._deal(42, "TST.US", C.MAGIC_NUMBER, fake.DEAL_ENTRY_OUT, 99.0, 1.0, -1.0, fake.DEAL_REASON_SL, fake.now)
        # 43: limit order filled while the bot was off, position still open
        bot.state["plans"]["43"] = {"key": "TST", "symbol": "TST.US", "dir": "long", "risk_money": 10.0,
                                    "status": "wartet", "placed_utc": fake.now}
        fake.positions[43] = SimpleNamespace(ticket=43, identifier=43, symbol="TST.US", magic=C.MAGIC_NUMBER,
                                             volume=1.0, type=0, price_open=100.0, sl=90.0, tp=120.0, comment="", time=0)
        bot.connect()
        bot.step()
        bot.step()
        journal = (Path(self.tmp.name) / C.JOURNAL_FILE).read_text(encoding="utf-8")
        self.assertEqual(journal.count(";CLOSE;"), 1)
        self.assertEqual(journal.count(";OPEN;"), 1)
        self.assertEqual(bot.state["plans"]["42"]["status"], "geschlossen")
        self.assertEqual(bot.state["plans"]["43"]["status"], "gefüllt")


class EarlyCloseEngine(unittest.TestCase):
    def test_engine_flattens_before_an_early_close(self):
        df = data.add_session_cols(data.make_demo("EC", days=30, seed=5, end="2026-11-27",
                                                  early_close={"2026-11-27": "13:00"}))
        res = analyze(df, "EC", CFG)
        for t in res["trades"]:
            if t["day"] == "2026-11-27":
                self.assertLessEqual(ny_minutes(t["exit_t"]), 12 * 60 + 50)


def ny_minutes(ts):
    t = pd.Timestamp(int(ts), unit="s", tz="UTC").tz_convert("America/New_York")
    return t.hour * 60 + t.minute


if __name__ == "__main__":
    unittest.main()

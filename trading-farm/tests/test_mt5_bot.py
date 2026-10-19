"""Live MT5 bot against a simulated broker (tests/fake_mt5.py).

Run with:  python -m unittest discover -s tests   (from the trading-farm folder)
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mt5"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mt5_config as C  # noqa: E402
from bot_smc import SmcBot, RET_DONE_PARTIAL  # noqa: E402
from fake_mt5 import FakeMT5  # noqa: E402
from farmlib import data  # noqa: E402
from farmlib.pipeline import analyze  # noqa: E402

CFG = json.loads((ROOT / "config.json").read_text())
# looser filters only for this test, so the short demo replay contains several setups -
# the test checks the bot's mechanics, not the strategy
CFG["engine"].update(use_htf_bias=False, mss_window=30, entry_window=24, max_risk_atr=6.0)
DAYS, HISTORY_DAYS = 10, 5


def find_busy_series():
    """A deterministic demo series whose replay window contains several setups."""
    for seed in range(100):
        df = data.make_demo("TST", days=DAYS, seed=seed)
        res = analyze(data.add_session_cols(df), "TST", CFG)
        start = df.t.iloc[HISTORY_DAYS * 78]
        armed = {x["armed_t"] for x in res["trades"] + res["refusals"] if x.get("armed_t") and x["armed_t"] > start}
        if len(armed) >= 2:
            return df, armed
    raise RuntimeError("no demo series with setups")


class LiveBot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df, cls.expected = find_busy_series()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.saved = {k: getattr(C, k) for k in ("MARKETS", "CLOCK_SYMBOLS", "BARS_HISTORY", "SERVER_UTC_OFFSET_HOURS")}
        C.MARKETS, C.CLOCK_SYMBOLS, C.BARS_HISTORY, C.SERVER_UTC_OFFSET_HOURS = {"TST": "TST.US"}, [], 5000, None
        cls.fake = FakeMT5({"TST.US": cls.df}, offset=3 * 3600)
        cls.bot = SmcBot(cls.fake, clock=lambda: cls.fake.now, cfg=CFG, workdir=Path(cls.tmp.name))
        cls.fake.advance_to(int(cls.df.t.iloc[0]))
        cls.bot.connect()
        cls.unprotected, cls.held_overnight = [], []
        start = HISTORY_DAYS * 78
        for i in range(start, len(cls.df)):
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
        for k, v in cls.saved.items():
            setattr(C, k, v)
        cls.tmp.cleanup()

    def placed(self):
        return [p for p in self.bot.state["plans"].values()]

    def test_detects_broker_time_offset(self):
        self.assertEqual(self.bot.offset, 3 * 3600)

    def test_orders_match_the_engine(self):
        placed = {p["armed_t"] for p in self.placed()}
        self.assertTrue(placed, "the bot never placed an order")
        self.assertTrue(placed <= self.expected, f"orders without engine setup: {placed - self.expected}")
        # every engine setup was placed unless a live guard said no (journal SKIP)
        journal = (Path(self.tmp.name) / C.JOURNAL_FILE).read_text(encoding="utf-8")
        self.assertGreaterEqual(len(placed) + journal.count(";SKIP;"), len(self.expected))

    def test_every_order_has_stop_and_target(self):
        for req in self.fake.sent:
            if req["action"] == FakeMT5.TRADE_ACTION_PENDING:
                self.assertTrue(req["sl"] > 0 and req["tp"] > 0)
                if req["type"] == FakeMT5.ORDER_TYPE_BUY_LIMIT:
                    self.assertTrue(req["sl"] < req["price"] < req["tp"])
                else:
                    self.assertTrue(req["tp"] < req["price"] < req["sl"])
        self.assertEqual(self.unprotected, [])

    def test_nothing_held_overnight(self):
        self.assertEqual(self.held_overnight, [])

    def test_one_position_or_order_per_symbol(self):
        self.assertLessEqual(len(self.fake.positions) + len(self.fake.orders), 1)

    def test_risk_is_about_one_percent(self):
        for p in self.placed():
            self.assertLessEqual(p["risk_money"], 10000 * 1.5 * C.RISK_PERCENT / 100 * 1.2)
            self.assertGreater(p["risk_money"], 0)

    def test_journal_written(self):
        journal = (Path(self.tmp.name) / C.JOURNAL_FILE).read_text(encoding="utf-8")
        self.assertIn(";ORDER;", journal)


class Safety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.df = data.make_demo("TST", days=3, seed=1)
        self.saved = {k: getattr(C, k) for k in ("MARKETS", "CLOCK_SYMBOLS")}
        C.MARKETS, C.CLOCK_SYMBOLS = {"TST": "TST.US"}, []

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(C, k, v)
        self.tmp.cleanup()

    def bot(self, **kw):
        fake = FakeMT5({"TST.US": self.df}, **kw)
        fake.advance_to(int(self.df.t.iloc[100]))
        return fake, SmcBot(fake, clock=lambda: fake.now, workdir=Path(self.tmp.name))

    def test_refuses_real_account(self):
        fake, bot = self.bot(demo=False)
        with self.assertRaises(SystemExit):
            bot.connect()

    def test_no_retry_after_partial_fill(self):
        fake, bot = self.bot()
        calls = []
        fake.order_send = lambda req: calls.append(req) or type("R", (), {"retcode": RET_DONE_PARTIAL})()
        bot.send({"action": 1}, [0, 1, 2])
        self.assertEqual(len(calls), 1)

    def test_daily_loss_limit_blocks_new_orders(self):
        fake, bot = self.bot()
        bot.connect()
        bot.daily_guard()
        fake.balance *= 1 - (C.DAILY_LOSS_LIMIT_PERCENT + 0.5) / 100
        bot.daily_guard()
        self.assertTrue(bot.state["kill"])
        n = len(fake.sent)
        bot.try_place("TST", "TST.US", {"dir": "long", "entry": 1, "sl": 0.9, "tp": 1.2, "armed_t": 1,
                                        "sweep_lb": "PDL", "sweep_p": 1, "mss_k": "mss", "mss_p": 1,
                                        "zone": {"type": "FVG", "bot": 1, "top": 1}})
        self.assertEqual(len(fake.sent), n)

    def test_unprotected_position_is_fixed_or_closed(self):
        fake, bot = self.bot()
        bot.connect()
        from types import SimpleNamespace
        fake.positions[1] = SimpleNamespace(ticket=1, identifier=1, symbol="TST.US", magic=C.MAGIC_NUMBER, volume=1.0,
                                            type=0, price_open=100.0, sl=0.0, tp=0.0, comment="", time=0)
        bot.guard_protection()
        self.assertNotIn(1, fake.positions)   # no plan known -> closed


if __name__ == "__main__":
    unittest.main()

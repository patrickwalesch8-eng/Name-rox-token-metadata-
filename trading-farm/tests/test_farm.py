"""Run with:  python -m unittest discover -s tests   (from the trading-farm folder)."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from farmlib import data  # noqa: E402
from farmlib.detect import detect  # noqa: E402
from farmlib.engine import run_engine  # noqa: E402

CFG = json.loads((ROOT / "config.json").read_text())


def strip(evs):
    keep = ("id", "k", "t", "d", "p", "top", "bot", "lb", "x")
    return [{k: e.get(k) for k in keep} for e in evs]


class NoLookahead(unittest.TestCase):
    """Whatever the farm knows at bar N must not change when later bars arrive."""

    def setUp(self):
        self.df = data.add_session_cols(data.make_demo("TEST", days=20, seed=7))

    def test_detector_is_causal(self):
        full, _ = detect(self.df, "TEST", "5m", CFG["detect"])
        for cut in (300, 777, 1200):
            part, _ = detect(self.df.iloc[:cut].reset_index(drop=True), "TEST", "5m", CFG["detect"])
            self.assertEqual(strip(part), strip([e for e in full if e["i"] < cut]), f"cut={cut}")

    def test_engine_is_causal(self):
        ev, a = detect(self.df, "TEST", "5m", CFG["detect"])
        trades, _ = run_engine(self.df, ev, a, "TEST", CFG["engine"], CFG["costs"])
        cut = 900
        day_end = self.df.t.values[cut - 1]
        dfp = self.df.iloc[:cut].reset_index(drop=True)
        evp, ap = detect(dfp, "TEST", "5m", CFG["detect"])
        tp, _ = run_engine(dfp, evp, ap, "TEST", CFG["engine"], CFG["costs"])
        done = [t for t in trades if t["exit_t"] < day_end]
        self.assertEqual([t["id"] for t in done], [t["id"] for t in tp if t["exit_t"] < day_end])
        self.assertEqual([t["net"] for t in done], [t["net"] for t in tp if t["exit_t"] < day_end])


class EngineChain(unittest.TestCase):
    def test_trades_are_consistent(self):
        df = data.add_session_cols(data.make_demo("CHAIN", days=60, seed=3))
        ev, a = detect(df, "CHAIN", "5m", CFG["detect"])
        trades, refusals = run_engine(df, ev, a, "CHAIN", CFG["engine"], CFG["costs"])
        self.assertTrue(refusals)
        by_id = {e["id"]: e for e in ev}
        for t in trades:
            sweep, mss = by_id[t["sweep"]], by_id[t["mss"]]
            self.assertLessEqual(sweep["t"], mss["t"])
            self.assertLess(mss["t"], t["fill_t"])           # never fill on the confirming bar
            self.assertLessEqual(t["fill_t"], t["exit_t"])
            self.assertGreaterEqual(t["r"], -1.0 - 1e-9)      # the stop caps the loss at 1R
            self.assertLessEqual(t["r"], CFG["engine"]["target_rr"] + 1e-9)
            self.assertLess(t["net"], t["r"])                 # costs always reduce the result
            if t["dir"] == "short":
                self.assertTrue(t["tp"] < t["entry"] < t["sl"])
            else:
                self.assertTrue(t["sl"] < t["entry"] < t["tp"])


class SessionEnd(unittest.TestCase):
    def test_flat_before_the_bell(self):
        """Positions are closed with the bar that ends at flat_at; nothing new starts there."""
        df = data.add_session_cols(data.make_demo("FLAT", days=60, seed=11))
        ev, a = detect(df, "FLAT", "5m", CFG["detect"])
        trades, refusals = run_engine(df, ev, a, "FLAT", CFG["engine"], CFG["costs"])
        hh, mm = CFG["engine"]["flat_at"].split(":")
        flat_bar_start = int(hh) * 60 + int(mm) - 5
        mins_of = dict(zip(df.t.values.tolist(), df.mins.values.tolist()))
        for t in trades:
            self.assertLessEqual(mins_of[t["exit_t"]], flat_bar_start)
            self.assertLessEqual(mins_of[t["fill_t"]], flat_bar_start)
            self.assertLess(mins_of[t["armed_t"]], flat_bar_start)
        for r in refusals:  # sweeps in the closing bars never start a candidate
            self.assertLess(mins_of[r["sweep_t"]], flat_bar_start)


class Aggregation(unittest.TestCase):
    def test_hour_bars_match_5m(self):
        df = data.add_session_cols(data.make_demo("AGG", days=3, seed=1))
        h1 = data.aggregate(df, "1h")
        self.assertEqual(len(h1), 3 * 7)                      # 09:30 ... 15:30 per day
        first = df[df.day == df.day.iloc[0]].iloc[:12]
        self.assertAlmostEqual(h1.h.iloc[0], first.h.max())
        self.assertAlmostEqual(h1.l.iloc[0], first.l.min())
        self.assertEqual(h1.v.iloc[0], first.v.sum())


if __name__ == "__main__":
    unittest.main()

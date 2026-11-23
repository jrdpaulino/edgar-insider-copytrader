"""Offline tests for the sweep. Run with: python -m unittest -v"""
import unittest
from datetime import date

from config import Config
from sweep import VARIANTS, format_table, run_sweep, summarize
from test_backtest import FakeBars, row


def ev(day, excess, h=20):
    return {"day": day, "ticker": "X", "why": "", "returns": {h: (excess, excess)}}


class SummarizeTests(unittest.TestCase):
    def test_basic_stats(self):
        events = [ev(date(2026, 1, 5), 0.10), ev(date(2026, 1, 6), -0.02), ev(date(2026, 2, 3), 0.04),
                  ev(date(2026, 3, 3), -0.06)]
        s = summarize(events, 20)
        self.assertEqual((s["n"], s["months"]), (4, 3))
        self.assertAlmostEqual(s["mean"], 0.015, places=6)
        self.assertAlmostEqual(s["median"], 0.01, places=6)
        self.assertEqual(s["beat"], 0.5)
        self.assertGreater(s["se"], 0)

    def test_empty_and_single_month(self):
        self.assertIsNone(summarize([], 20))
        self.assertIsNone(summarize([ev(date(2026, 1, 5), 0.1)], 90))      # no 90-day return available
        s = summarize([ev(date(2026, 1, 5), 0.1), ev(date(2026, 1, 9), 0.2)], 20)
        self.assertNotEqual(s["se"], s["se"])                                # NaN with one month only


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            row("1", "a1"), row("2", "a2"),                                                 # March: in train
            row("3", "b1", filed="2026-08-10", tx="2026-08-09"),
            row("4", "b2", filed="2026-08-10", tx="2026-08-09"),                            # August: in holdout
        ]

    def sweep(self, variants=VARIANTS):
        return run_sweep(self.rows, Config(), FakeBars(), date(2026, 3, 9), date(2026, 8, 31),
                         date(2026, 6, 1), horizon=20, variants=variants)

    def test_train_and_holdout_are_separated(self):
        base = self.sweep()[0]
        self.assertEqual(base["name"], "baseline (.env settings)")
        self.assertEqual((base["signals"], base["train"]["n"], base["holdout"]["n"]), (2, 1, 1))
        expected = 1.01 ** 20 - 1    # FakeBars: XYZ +1% per day, SPY flat
        self.assertAlmostEqual(base["train"]["mean"], expected, places=6)
        self.assertAlmostEqual(base["holdout"]["mean"], expected, places=6)

    def test_stricter_variant_can_remove_all_signals(self):
        three = [r for r in self.sweep() if r["name"] == "3+ insiders"][0]
        self.assertEqual((three["signals"], three["train"], three["holdout"]), (0, None, None))

    def test_table_renders_even_with_empty_variants(self):
        text = format_table(self.sweep(), date(2026, 6, 1), 20, "SPY")
        self.assertIn("baseline (.env settings)", text)
        self.assertIn("3+ insiders", text)
        self.assertIn("HOLDOUT", text)
        self.assertIn("8 variants were tested", text)


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the multi-signal framework. Run with: python -m unittest -v"""
import os
import random
import tempfile
import unittest
from array import array
import math
from datetime import date, timedelta

from multi.alpaca_data import download_bars, download_news, effective_day
from multi.engine import Panel, Params, build_panel, format_report, summarize
from multi.features import (NAN, NewsBuzz, average_ranks, buzz_series, fwd_return, high_52w, isnan, momentum_12_1,
                            rank_norm, spearman, trading_day_counts, trend_200d)
from multi.store import PanelStore


def weekdays(n, start=date(2020, 1, 6)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def make_data(n_days=800, n_stocks=120, drift_sd=0.0012, noise=0.012, seed=1, bench_noise=0.0):
    """Stocks with a persistent daily drift (planted effect) plus noise. Benchmark is flat on average.

    bench_noise > 0 adds a common market factor (beta 1 for every stock) and makes the benchmark follow it."""
    rng = random.Random(seed)
    mkt = [random.Random(seed + 1000).gauss(0, bench_noise) for _ in range(n_days)] if bench_noise else [0.0] * n_days
    days = weekdays(n_days)
    opens, closes, vols = {}, {}, {}
    for k in range(n_stocks):
        mu, price = rng.gauss(0, drift_sd), 50.0
        o, c = array("d"), array("d")
        for _ in range(n_days):
            o.append(price)
            price *= 1 + mu + mkt[len(c)] + rng.gauss(0, noise)
            c.append(price)
        opens[f"S{k:03d}"], closes[f"S{k:03d}"] = o, c
        vols[f"S{k:03d}"] = array("d", [1e6]) * n_days
    bo, bc, level = array("d"), array("d"), 100.0
    for m in mkt:
        bo.append(level)
        level *= 1 + m
        bc.append(level)
    return days, opens, closes, vols, bo, bc


def make_panel(data, **kw):
    days, o, c, v, bo, bc = data
    params = Params(top_n=15, min_stocks=50, min_dollar_volume=1000.0, **kw)
    return Panel(days, o, c, v, bo, bc, params)


class FeatureTests(unittest.TestCase):
    def test_ranks_and_spearman(self):
        self.assertEqual(average_ranks([10, 20, 20, 30]), [1.0, 2.5, 2.5, 4.0])
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertTrue(isnan(spearman([1, 1, 1, 1], [1, 2, 3, 4])))
        rn = rank_norm([5, 1, 3])
        self.assertTrue(rn[1] < rn[2] < rn[0])

    def test_price_features(self):
        c = array("d", [100.0 * 1.001 ** k for k in range(300)])
        self.assertAlmostEqual(momentum_12_1(c, 280), c[259] / c[28] - 1)
        self.assertTrue(isnan(momentum_12_1(c, 100)))
        self.assertGreater(trend_200d(c, 280), 0)
        self.assertAlmostEqual(high_52w(c, 280), 0.0)

    def test_forward_return_uses_next_open(self):
        o = array("d", [1, 2, 4, 4, 4, 4])
        c = array("d", [1, 2, 4, 4, 4, 8])
        self.assertAlmostEqual(fwd_return(o, c, 0, 4), 4 / 2 - 1)   # enter at o[1]=2, exit at c[4]=4
        self.assertTrue(isnan(fwd_return(o, c, 3, 4)))

    def test_effective_day(self):
        self.assertEqual(effective_day("2026-03-02T15:00:00Z"), "2026-03-02")   # 10:00 ET, during the session
        self.assertEqual(effective_day("2026-03-02T21:30:00Z"), "2026-03-03")   # 16:30 ET, after the close
        self.assertIsNone(effective_day("garbage"))

    def test_weekend_news_maps_to_monday(self):
        days = ["2026-03-06", "2026-03-09", "2026-03-10"]            # Fri, Mon, Tue
        arr = trading_day_counts({"2026-03-07": 2, "2026-03-08": 3, "2026-03-09": 1}, days)
        self.assertEqual(list(arr), [0.0, 6.0, 0.0])

    def test_buzz_spike(self):
        counts = array("d", [1.0]) * 120
        for k in (100, 101, 102):
            counts[k] = 15
        z = buzz_series(counts)
        self.assertGreater(z[102], 5)
        self.assertTrue(abs(z[90]) < 1.5)
        self.assertTrue(isnan(buzz_series(array("d", [0.0]) * 120)[100]))   # too little news -> undefined

    def test_credibility_uses_only_known_outcomes(self):
        n, h = 200, 20
        counts = array("d", [1.0]) * n
        for k in (98, 99, 100):
            counts[k] = 15
        closes = array("d", [100.0]) * n
        closes[100], closes[101], closes[102] = 102.0, 104.0, 106.0
        for k in range(103, n):
            closes[k] = 106.0 * (1 + 0.01 * (k - 102))
        opens = array("d", closes)
        nb = NewsBuzz(counts, opens, closes, array("d", [0.0]) * n, h)
        self.assertEqual(nb.known_idx, [100 + h])
        self.assertEqual(nb.credibility(100 + h - 1), 0.5)          # outcome not known yet: no credit
        self.assertGreater(nb.credibility(100 + h), 0.5)            # known now: follow-through earns credit


ALL7 = ("momentum", "trend", "high52", "reversal", "lowvol", "resmom", "volspike")


class NewPriceSignalTests(unittest.TestCase):
    def extras(self, c, v, bc):
        from multi.features import BenchStats, PriceExtras
        return PriceExtras(c, v, BenchStats(bc))

    def test_reversal_prefers_losers(self):
        from multi.features import reversal_1m
        down = array("d", [100.0 - k for k in range(40)])
        up = array("d", [100.0 + k for k in range(40)])
        self.assertGreater(reversal_1m(down, 30), reversal_1m(up, 30))

    def test_lowvol_prefers_calm(self):
        rng = random.Random(4)
        n, calm, wild = 120, array("d", [100.0]), array("d", [100.0])
        for _ in range(n - 1):
            calm.append(calm[-1] * (1 + rng.gauss(0, 0.005)))
            wild.append(wild[-1] * (1 + rng.gauss(0, 0.03)))
        bc = array("d", [100.0 + 0.1 * k for k in range(n)])
        v = array("d", [1e6]) * n
        self.assertGreater(self.extras(calm, v, bc).lowvol(110), self.extras(wild, v, bc).lowvol(110))

    def test_resmom_removes_market_exposure(self):
        rng = random.Random(5)
        n = 300
        mkt = [rng.gauss(0.0005, 0.01) for _ in range(n)]
        bc, hi_beta, flat = array("d", [100.0]), array("d", [50.0]), array("d", [50.0])
        for m in mkt[1:]:
            bc.append(bc[-1] * (1 + m))
            hi_beta.append(hi_beta[-1] * (1 + 2.0 * m))               # pure market exposure, no alpha
            flat.append(flat[-1] * (1 + 1.0 * m + 0.002))             # market + steady alpha
        v = array("d", [1e6]) * n
        a, b = self.extras(hi_beta, v, bc).resmom(280), self.extras(flat, v, bc).resmom(280)
        self.assertAlmostEqual(a, 0.0, delta=0.02)                    # beta explains it all
        self.assertGreater(b, 0.3)                                    # ~231 days x 0.2% of alpha
        self.assertTrue(isnan(self.extras(flat, v, bc).resmom(100)))  # not enough history

    def test_volspike(self):
        n = 120
        c = array("d", [10.0]) * n
        v = array("d", [1e6]) * n
        for k in range(115, 120):
            v[k] = 4e6
        ex = self.extras(c, v, array("d", [100.0]) * n)
        self.assertAlmostEqual(ex.volspike(119), math.log(4.0))
        self.assertAlmostEqual(ex.volspike(110), 0.0)

    def test_missing_day_blanks_the_window(self):
        n = 300
        c = array("d", [100.0 + 0.1 * k for k in range(n)])
        c[200] = NAN
        ex = self.extras(c, array("d", [1e6]) * n, array("d", [100.0 + k for k in range(n)]))
        self.assertTrue(isnan(ex.lowvol(230)))
        self.assertFalse(isnan(ex.lowvol(290)))


class NoLookaheadTest(unittest.TestCase):
    def test_scrambling_the_future_changes_nothing(self):
        data = make_data(n_days=500, n_stocks=80, seed=3, bench_noise=0.008)
        i = 420
        base = make_panel(data, signals=ALL7)
        w0, status0 = base.weights_at(i)
        port0 = base.portfolio(i, w0 or {s: 1 / 7 for s in base.p.signals})
        rng = random.Random(9)
        days, o, c, v, bo, bc = data
        o2 = {s: array("d", a) for s, a in o.items()}
        c2 = {s: array("d", a) for s, a in c.items()}
        bo2, bc2 = array("d", bo), array("d", bc)
        for s in c2:
            for k in range(i + 2, len(days)):                      # day i+1's open is kept: it is the entry price
                o2[s][k] *= rng.uniform(0.5, 2)
                c2[s][k] *= rng.uniform(0.5, 2)
        for k in range(i + 2, len(days)):
            bo2[k] *= rng.uniform(0.5, 2)
            bc2[k] *= rng.uniform(0.5, 2)
        other = make_panel((days, o2, c2, v, bo2, bc2), signals=ALL7)
        w1, status1 = other.weights_at(i)
        self.assertEqual(status0, status1)
        self.assertEqual(w0, w1)
        self.assertEqual(port0, other.portfolio(i, w1 or {s: 1 / 7 for s in other.p.signals}))


class EngineTests(unittest.TestCase):
    def test_planted_momentum_is_found(self):
        rows = make_panel(make_data(seed=1)).run(progress=lambda m: None)
        mom = summarize(rows, "only:momentum")
        ad = summarize(rows, "adaptive")
        self.assertGreater(mom["n"], 15)
        self.assertGreater(mom["mean"], 3 * mom["se"])              # clearly positive
        self.assertGreater(ad["mean"], 0)
        self.assertLess(ad["abstained"], ad["n"] // 2)

    def test_seven_signals_run_and_weights_valid(self):
        panel = make_panel(make_data(seed=1, bench_noise=0.008), signals=ALL7)
        rows = panel.run(progress=lambda m: None)
        self.assertGreater(summarize(rows, "adaptive")["n"], 10)
        for r in rows:
            if r["weights"]:
                self.assertEqual(set(r["weights"]), set(ALL7))
                self.assertAlmostEqual(sum(r["weights"].values()), 1.0)
                self.assertTrue(all(x >= 0 for x in r["weights"].values()))

    def test_signal_without_history_sits_out(self):
        panel = make_panel(make_data(seed=1), signals=("momentum", "trend", "volspike"))   # constant volume: no IC
        rows = panel.run(progress=lambda m: None)
        ws = [r["weights"] for r in rows if r["weights"]]
        self.assertTrue(ws)
        for w in ws:
            self.assertEqual(w["volspike"], 0.0)
            self.assertAlmostEqual(sum(w.values()), 1.0)

    def test_pure_noise_shows_no_edge(self):
        rows = make_panel(make_data(drift_sd=0.0, seed=2)).run(progress=lambda m: None)
        ad = summarize(rows, "adaptive")
        self.assertGreater(ad["n"], 15)
        self.assertLess(ad["mean"], 3 * ad["se"])                   # nothing significantly positive

    def test_report_and_weights(self):
        panel = make_panel(make_data(seed=1))
        rows = panel.run(progress=lambda m: None)
        text = format_report(rows, panel.p, "IWM", eval_start=rows[len(rows) // 2]["day"])
        self.assertIn("HOLDOUT", text)
        self.assertIn("adaptive average weights", text)
        weights = [r["weights"] for r in rows if r["weights"]]
        for w in weights:
            self.assertAlmostEqual(sum(w.values()), 1.0)
            self.assertTrue(all(x >= 0 for x in w.values()))

    def test_store_roundtrip_and_build_panel(self):
        days, o, c, v, bo, bc = make_data(n_days=300, n_stocks=5)
        store = PanelStore(os.path.join(tempfile.mkdtemp(), "p.sqlite"))
        store.save_symbol("IWM", "kept", [(d, 100.0, 100.0, 1e6) for d in days])
        for s in o:
            store.save_symbol(s, "kept", [(d, o[s][k], c[s][k], 1e6) for k, d in enumerate(days)])
        store.save_symbol("BAD", "short")
        store.commit()
        panel = build_panel(store, "IWM", Params(min_stocks=3))
        self.assertEqual(len(panel.syms), 5)
        self.assertEqual(len(panel.days), 300)


class FakeClient:
    def __init__(self):
        self.news_calls = []

    def bars(self, symbols, start, end):
        days = weekdays(300)
        out = {}
        for s in symbols:
            if s == "SHORT":
                out[s] = [(d, 10, 10, 1e6) for d in days[:50]]
            elif s == "THIN":
                out[s] = [(d, 10, 10, 1) for d in days]
            elif s != "MISSING":
                out[s] = [(d, 10, 10, 1e6) for d in days]
        return out

    def news_day(self, day, max_symbols=5):
        self.news_calls.append(day)
        return {(day.isoformat(), "AAA"): 2}, 2


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.store = PanelStore(os.path.join(tempfile.mkdtemp(), "p.sqlite"))

    def test_screening_and_resume(self):
        client = FakeClient()
        syms = ["GOOD", "SHORT", "THIN", "MISSING"]
        download_bars(client, self.store, syms, date(2025, 1, 1), date(2026, 1, 1), progress=lambda m: None)
        st = self.store.statuses()
        self.assertEqual((st["GOOD"], st["SHORT"], st["THIN"], st["MISSING"]), ("kept", "short", "illiquid", "short"))
        self.assertEqual(len(self.store.load_bars("GOOD")), 300)
        self.assertEqual(self.store.load_bars("THIN"), [])

    def test_news_is_resumable_and_not_double_counted(self):
        client = FakeClient()
        download_news(client, self.store, date(2026, 3, 1), date(2026, 3, 5), progress=lambda m: None)
        self.assertEqual(len(client.news_calls), 5)
        download_news(client, self.store, date(2026, 3, 1), date(2026, 3, 7), progress=lambda m: None)
        self.assertEqual(len(client.news_calls), 7)                 # only the two new days were fetched
        self.assertEqual(self.store.load_news()["AAA"]["2026-03-03"], 2)
        self.assertEqual(self.store.news_range(), ("2026-03-01", "2026-03-07"))


if __name__ == "__main__":
    unittest.main()

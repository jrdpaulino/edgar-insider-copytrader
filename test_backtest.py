"""Offline tests for the backtest. Run with: python -m unittest -v"""
import os
import tempfile
import unittest
import zipfile
from datetime import date, timedelta

from backtest import forward_returns, load_dataset, run_backtest, signal_events
from config import Config


def business_days(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def tsv(rows):
    cols = list(rows[0])
    return "\n".join(["\t".join(cols)] + ["\t".join(str(r[c]) for c in cols) for r in rows]) + "\n"


class DatasetTests(unittest.TestCase):
    def make_zip(self):
        path = os.path.join(tempfile.mkdtemp(), "2026q4_form345.zip")
        sub = [
            {"ACCESSION_NUMBER": "A1", "FILING_DATE": "10-NOV-2026", "DOCUMENT_TYPE": "4", "ISSUERCIK": "1",
             "ISSUERNAME": "ACME CORP", "ISSUERTRADINGSYMBOL": "acme"},
            {"ACCESSION_NUMBER": "A2", "FILING_DATE": "10-NOV-2026", "DOCUMENT_TYPE": "4/A", "ISSUERCIK": "1",
             "ISSUERNAME": "ACME CORP", "ISSUERTRADINGSYMBOL": "ACME"},
        ]
        own = [
            {"ACCESSION_NUMBER": "A1", "RPTOWNERCIK": "9", "RPTOWNERNAME": "Doe Jane",
             "RPTOWNER_RELATIONSHIP": "Director,Officer", "RPTOWNER_TITLE": "CEO"},
            {"ACCESSION_NUMBER": "A2", "RPTOWNERCIK": "9", "RPTOWNERNAME": "Doe Jane",
             "RPTOWNER_RELATIONSHIP": "Officer", "RPTOWNER_TITLE": "CEO"},
        ]
        tr = [
            {"ACCESSION_NUMBER": "A1", "TRANS_DATE": "09-NOV-2026", "TRANS_CODE": "P", "EQUITY_SWAP_INVOLVED": "0",
             "TRANS_SHARES": "1000", "TRANS_PRICEPERSHARE": "10.00", "TRANS_ACQUIRED_DISP_CD": "A"},
            {"ACCESSION_NUMBER": "A1", "TRANS_DATE": "09-NOV-2026", "TRANS_CODE": "S", "EQUITY_SWAP_INVOLVED": "0",
             "TRANS_SHARES": "500", "TRANS_PRICEPERSHARE": "10.00", "TRANS_ACQUIRED_DISP_CD": "D"},
            {"ACCESSION_NUMBER": "A2", "TRANS_DATE": "09-NOV-2026", "TRANS_CODE": "P", "EQUITY_SWAP_INVOLVED": "0",
             "TRANS_SHARES": "1000", "TRANS_PRICEPERSHARE": "10.00", "TRANS_ACQUIRED_DISP_CD": "A"},
        ]
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("SUBMISSION.tsv", tsv(sub))
            z.writestr("REPORTINGOWNER.tsv", tsv(own))
            z.writestr("NONDERIV_TRANS.tsv", tsv(tr))
        return path

    def test_load_dataset(self):
        rows = load_dataset(self.make_zip())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["symbol"], r["owner_name"], r["value"]), ("ACME", "Doe Jane", 10000.0))
        self.assertEqual((r["is_officer"], r["is_director"], r["is_ten_pct"]), (1, 1, 0))
        self.assertEqual((r["tx_date"], r["filed"]), ("2026-11-09", "2026-11-10"))


class FakeBars:
    """XYZ rises 1% per trading day; SPY is flat. Open == close."""

    def __init__(self):
        self.days = business_days(date(2026, 1, 1), date(2026, 12, 31))

    def get(self, symbol, start, end):
        if symbol == "SPY":
            return [(d, 100.0, 100.0) for d in self.days]
        if symbol == "XYZ":
            return [(d, 100 * 1.01 ** i, 100 * 1.01 ** i) for i, d in enumerate(self.days)]
        return []


def row(who, acc, filed="2026-03-10", tx="2026-03-09", sym="XYZ"):
    return {"accession": acc, "issuer_cik": "1", "symbol": sym, "issuer_name": sym, "owner_cik": who,
            "owner_name": f"Insider {who}", "title": "Director", "is_officer": 0, "is_director": 1,
            "is_ten_pct": 0, "tx_date": tx, "filed": filed, "shares": 10000.0, "price": 10.0,
            "value": 100000.0, "tenb5": 0}


class BacktestTests(unittest.TestCase):
    def test_one_signal_no_repeat_while_held(self):
        rows = [row("1", "a1"), row("2", "a2"), row("3", "a3", filed="2026-03-12", tx="2026-03-11")]
        events = signal_events(rows, Config(), date(2026, 3, 9), date(2026, 4, 30))
        self.assertEqual([(d, s.ticker) for d, s in events], [(date(2026, 3, 10), "XYZ")])

    def test_returns_measured_from_next_day_open_vs_benchmark(self):
        rows = [row("1", "a1"), row("2", "a2")]
        res = run_backtest(rows, Config(), FakeBars(), date(2026, 3, 9), date(2026, 3, 20))
        self.assertEqual((res["signals"], res["skipped_no_prices"]), (1, 0))
        self.assertAlmostEqual(res["summary"][5]["mean_return"], 1.01 ** 5 - 1, places=6)
        self.assertAlmostEqual(res["summary"][20]["mean_excess"], 1.01 ** 20 - 1, places=6)
        self.assertEqual(res["summary"][5]["beat_benchmark"], 1.0)

    def test_signal_without_price_data_is_skipped(self):
        rows = [row("1", "a1", sym="NOPE"), row("2", "a2", sym="NOPE")]
        res = run_backtest(rows, Config(), FakeBars(), date(2026, 3, 9), date(2026, 3, 20))
        self.assertEqual((res["signals"], res["skipped_no_prices"], res["summary"]), (1, 1, {}))

    def test_horizon_beyond_available_bars_is_omitted(self):
        bars = FakeBars()
        short = bars.get("XYZ", None, None)[:70]
        out = forward_returns(short, bars.get("SPY", None, None), date(2026, 3, 10), (5, 90))
        self.assertEqual(sorted(out), [5])


if __name__ == "__main__":
    unittest.main()

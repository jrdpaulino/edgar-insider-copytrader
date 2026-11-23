"""Offline tests for the price cache. Run with: python -m unittest -v"""
import json
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

from backtest import AlpacaBars


class FakeResp:
    status_code = 200

    def json(self):
        return {"bars": [{"t": "2026-03-02T05:00:00Z", "o": 10.0, "c": 11.0}], "next_page_token": None}

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self):
        self.headers, self.calls = {}, 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return FakeResp()


@mock.patch("backtest.time.sleep", lambda s: None)
class BarsCacheTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "bars.json")
        self.sess = FakeSession()

    def bars(self, **kw):
        return AlpacaBars("key", "secret", cache_path=self.path, session=self.sess, **kw)

    def test_saves_in_batches_not_every_fetch(self):
        b = self.bars(save_every=3)
        for sym in ("AAA", "BBB"):
            b.get(sym, date(2026, 1, 1), date(2026, 6, 1))
        self.assertFalse(os.path.exists(self.path))          # two fetches: still in memory only
        b.get("CCC", date(2026, 1, 1), date(2026, 6, 1))
        with open(self.path) as fh:
            self.assertEqual(len(json.load(fh)), 3)          # third fetch triggers a save

    def test_final_save_and_reload_skips_downloads(self):
        b = self.bars(save_every=25)
        for sym in ("AAA", "BBB"):
            b.get(sym, date(2026, 1, 1), date(2026, 6, 1))
        b.save()
        self.assertFalse(os.path.exists(self.path + ".tmp"))  # no temp file left behind
        calls = self.sess.calls
        again = self.bars()
        rows = again.get("AAA", date(2026, 1, 1), date(2026, 6, 1))
        self.assertEqual(self.sess.calls, calls)             # served from the cache
        self.assertEqual(rows, [(date(2026, 3, 2), 10.0, 11.0)])

    def test_save_with_nothing_new_is_a_noop(self):
        self.bars().save()
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()

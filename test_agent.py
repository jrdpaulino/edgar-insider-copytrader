"""Offline tests for signals, risk and the daily run. Run with: python -m unittest -v"""
import os
import tempfile
import unittest
from datetime import date, timedelta

from agent import run_once
from broker import BrokerError
from config import Config
from signals import select, summarize_rejections
from state import State

TODAY = date(2026, 11, 16)


def buy(sym="ACME", who="1", name=None, title="Chief Executive Officer", officer=1, director=0, ten=0,
        shares=10000, price=12.5, tx=2, filed=1, tenb5=0):
    return {"accession": f"{sym}-{who}", "issuer_cik": "1", "symbol": sym, "issuer_name": f"{sym} Corp",
            "owner_cik": who, "owner_name": name or f"Insider {who}", "title": title, "is_officer": officer,
            "is_director": director, "is_ten_pct": ten, "tx_date": str(TODAY - timedelta(days=tx)),
            "filed": str(TODAY - timedelta(days=filed)), "shares": shares, "price": price,
            "value": shares * price, "tenb5": tenb5}


def cluster(sym="ACME"):
    return [buy(sym, "1"), buy(sym, "2", title="Director", officer=0, director=1)]


class FakeBroker:
    def __init__(self, is_open=True, positions=None, equity=100000.0, fail=False, prices=None, default_price=12.5):
        self.is_open, self._pos, self.equity, self.fail = is_open, positions or {}, equity, fail
        self.prices, self.default_price = prices or {}, default_price
        self.orders, self.closed, self.clock_calls = [], [], 0

    def account(self):
        return {"equity": str(self.equity), "buying_power": str(self.equity), "trading_blocked": False}

    def clock(self):
        self.clock_calls += 1
        return {"is_open": self.is_open}

    def positions(self):
        return dict(self._pos)

    def asset(self, symbol):
        if symbol == "ZZZZ":
            return None
        return {"status": "active", "tradable": True, "fractionable": True}

    def latest_price(self, symbol):
        price = self.prices.get(symbol, self.default_price)
        if price is None:
            raise BrokerError("no price")
        return price

    def market_order(self, symbol, side, notional, client_order_id):
        if self.fail:
            raise BrokerError("boom")
        self.orders.append((symbol, side, notional, client_order_id))
        return {"id": f"order-{len(self.orders)}"}

    def close_position(self, symbol):
        self.closed.append(symbol)
        return {"id": "close-1"}


class SignalTests(unittest.TestCase):
    def cfg(self, **kw):
        return Config(**kw)

    def test_cluster_qualifies(self):
        sigs, _ = select(cluster(), self.cfg(), TODAY)
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertEqual((s.ticker, s.n_insiders, s.total_usd, s.avg_price), ("ACME", 2, 250000.0, 12.5))
        self.assertEqual(s.notional_usd, 250.0)

    def test_single_insider_below_threshold(self):
        sigs, rej = select([buy(title="Director", officer=0, director=1)], self.cfg(), TODAY)
        self.assertEqual(sigs, [])
        self.assertEqual(summarize_rejections(rej), {"below cluster threshold": 1})

    def test_small_cluster_below_dollar_threshold(self):
        rows = [buy("ACME", "1", shares=100), buy("ACME", "2", shares=100)]   # $2,500 total
        self.assertEqual(select(rows, self.cfg(), TODAY)[0], [])

    def test_ceo_alone_needs_large_purchase(self):
        self.assertEqual(len(select([buy(shares=30000)], self.cfg(), TODAY)[0]), 1)      # $375k
        self.assertEqual(select([buy(shares=10000)], self.cfg(), TODAY)[0], [])          # $125k

    def test_vice_president_is_not_ceo(self):
        rows = [buy(title="Vice President of Sales", shares=40000)]
        self.assertEqual(select(rows, self.cfg(), TODAY)[0], [])
        for title in ("President and CEO", "EVP & Chief Financial Officer", "President"):
            self.assertEqual(len(select([buy(title=title, shares=40000)], self.cfg(), TODAY)[0]), 1, title)

    def test_filters(self):
        cases = {
            "ten percent owner": [buy(who=str(i), officer=0, director=0, ten=1) for i in (1, 2)],
            "10b5-1": [buy(who=str(i), tenb5=1) for i in (1, 2)],
            "low price": [buy(who=str(i), price=2.0, shares=50000) for i in (1, 2)],
            "stale trade": [buy(who=str(i), tx=30) for i in (1, 2)],
            "stale filing": [buy(who=str(i), filed=30) for i in (1, 2)],
            "bad ticker": [buy("bad!", who=str(i)) for i in (1, 2)],
        }
        for name, rows in cases.items():
            self.assertEqual(select(rows, self.cfg(), TODAY)[0], [], name)

    def test_blocklist_held_and_seen(self):
        self.assertEqual(select(cluster(), self.cfg(ticker_blocklist=("ACME",)), TODAY)[0], [])
        self.assertEqual(select(cluster(), self.cfg(), TODAY, held={"ACME"})[0], [])
        self.assertEqual(select(cluster(), self.cfg(), TODAY, seen_ids={"ACME-1", "ACME-2"})[0], [])
        self.assertEqual(len(select(cluster(), self.cfg(), TODAY, seen_ids={"ACME-1"})[0]), 1)

    def test_ranked_by_insiders_then_dollars(self):
        rows = cluster("AAA") + cluster("BBB") + [buy("BBB", "3", title="CFO")]
        self.assertEqual([s.ticker for s in select(rows, self.cfg(), TODAY)[0]], ["BBB", "AAA"])


class AgentBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = State(":memory:")
        self.collector_calls = 0

    def collector(self, state, today):
        self.collector_calls += 1
        return {"fake": True}

    def cfg(self, **kw):
        kw.setdefault("kill_switch_path", os.path.join(self.tmp, "STOP"))
        kw.setdefault("dry_run", False)
        return Config(**kw)

    def run_agent(self, broker, cfg=None, **kw):
        return run_once(cfg or self.cfg(), self.collector, broker, self.state, today=TODAY, **kw)


class AgentTests(AgentBase):
    def test_cluster_buys_once_and_opens_lot(self):
        self.state.add_buys(cluster())
        b = FakeBroker()
        s = self.run_agent(b)
        self.assertEqual((s["signals"], s["orders"]), (1, 1))
        self.assertEqual(b.orders[0][:3], ("ACME", "buy", 250.0))
        self.assertEqual(self.state.open_lot_symbols(), {"ACME"})
        self.run_agent(b)
        self.assertEqual(len(b.orders), 1)          # held / already processed -> no second buy

    def test_dry_run_places_and_records_nothing(self):
        self.state.add_buys(cluster())
        b = FakeBroker()
        s = self.run_agent(b, self.cfg(dry_run=True))
        self.assertEqual(s["orders"], 1)
        self.assertEqual((b.orders, self.state.open_lots(), self.state.seen_ids()), ([], [], set()))

    def test_price_chase_defers_then_buys_when_price_falls(self):
        self.state.add_buys(cluster())
        b = FakeBroker(prices={"ACME": 20.0})      # 60% above the insiders' $12.50
        s = self.run_agent(b)
        self.assertEqual((s["deferred"], b.orders, self.state.seen_ids()), (1, [], set()))
        b.prices["ACME"] = 13.0
        self.run_agent(b)
        self.assertEqual(len(b.orders), 1)

    def test_missing_price_defers(self):
        self.state.add_buys(cluster())
        b = FakeBroker(prices={"ACME": None})
        self.assertEqual(self.run_agent(b)["deferred"], 1)
        self.assertEqual(b.orders, [])

    def test_ticker_already_in_account_is_rejected_for_good(self):
        self.state.add_buys(cluster())
        b = FakeBroker(positions={"ACME": 500.0})
        s = self.run_agent(b)
        self.assertEqual((s["rejected"], b.orders), (1, []))
        self.assertEqual(self.run_agent(b)["signals"], 0)

    def test_untradable_asset_rejected(self):
        self.state.add_buys(cluster("ZZZZ"))
        self.assertEqual(self.run_agent(FakeBroker())["rejected"], 1)

    def test_position_cap_rejects(self):
        self.state.add_buys(cluster())
        b = FakeBroker(equity=1000.0)               # $250 would be 25% of equity
        self.assertEqual(self.run_agent(b)["rejected"], 1)
        self.assertEqual(b.orders, [])

    def test_max_new_per_run(self):
        self.state.add_buys(cluster("AAA") + cluster("BBB") + cluster("CCC"))
        b = FakeBroker()
        s = self.run_agent(b, self.cfg(max_new_per_run=2))
        self.assertEqual((s["orders"], s["deferred"]), (2, 1))
        self.run_agent(b, self.cfg(max_new_per_run=2))
        self.assertEqual(sorted(o[0] for o in b.orders), ["AAA", "BBB", "CCC"])   # leftover picked up next run

    def test_daily_cap(self):
        self.state.add_buys(cluster("AAA") + cluster("BBB") + cluster("CCC"))
        b = FakeBroker()
        s = self.run_agent(b, self.cfg(max_order_usd=250, max_daily_usd=500))
        self.assertEqual((s["orders"], s["deferred"]), (2, 1))

    def test_kill_switch(self):
        self.state.add_buys(cluster())
        open(os.path.join(self.tmp, "STOP"), "w").close()
        b = FakeBroker()
        s = self.run_agent(b)
        self.assertEqual((s["halted"], b.orders, self.collector_calls), (True, [], 0))

    def test_market_closed_collects_but_does_not_trade(self):
        self.state.add_buys(cluster())
        self.state.open_lot("OLD", TODAY - timedelta(days=95), 250, "x")
        b = FakeBroker(is_open=False, positions={"OLD": 300.0})
        s = self.run_agent(b)
        self.assertEqual((s["market_closed"], b.orders, b.closed, self.collector_calls), (True, [], [], 1))
        self.assertEqual(self.state.seen_ids(), set())

    def test_collect_only(self):
        self.state.add_buys(cluster())
        b = FakeBroker()
        s = self.run_agent(b, collect_only=True)
        self.assertEqual((s["signals"], b.orders, b.clock_calls), (0, [], 0))

    def test_broker_failure_keeps_signal_pending(self):
        self.state.add_buys(cluster())
        self.run_agent(FakeBroker(fail=True))
        self.assertEqual((self.state.seen_ids(), self.state.open_lots()), (set(), []))


class ExitTests(AgentBase):
    def test_closes_only_lots_past_hold_period(self):
        self.state.open_lot("OLD", TODAY - timedelta(days=95), 250, "x")
        self.state.open_lot("NEW", TODAY - timedelta(days=10), 250, "y")
        b = FakeBroker(positions={"OLD": 300.0, "NEW": 260.0, "MANUAL": 5000.0})
        s = self.run_agent(b)
        self.assertEqual((s["exits"], b.closed), (1, ["OLD"]))
        self.assertEqual(self.state.open_lot_symbols(), {"NEW"})

    def test_lot_with_missing_position_is_closed_without_selling(self):
        self.state.open_lot("GONE", TODAY - timedelta(days=95), 250, "x")
        b = FakeBroker(positions={})
        self.run_agent(b)
        self.assertEqual((b.closed, self.state.open_lots()), ([], []))

    def test_dry_run_never_sells(self):
        self.state.open_lot("OLD", TODAY - timedelta(days=95), 250, "x")
        b = FakeBroker(positions={"OLD": 300.0})
        self.run_agent(b, self.cfg(dry_run=True))
        self.assertEqual((b.closed, self.state.open_lot_symbols()), ([], {"OLD"}))

    def test_sell_failure_keeps_lot_open(self):
        self.state.open_lot("OLD", TODAY - timedelta(days=95), 250, "x")

        class Boom(FakeBroker):
            def close_position(self, symbol):
                raise BrokerError("nope")
        self.run_agent(Boom(positions={"OLD": 300.0}))
        self.assertEqual(self.state.open_lot_symbols(), {"OLD"})


if __name__ == "__main__":
    unittest.main()

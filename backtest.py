"""Backtest the insider-cluster signal on SEC's quarterly "Insider Transactions Data Sets".

1. Download one or more quarterly zips (e.g. 2025q1_form345.zip) from the SEC's
   "Insider Transactions Data Sets" page and pass them with --datasets.
2. The same signal rules the live agent uses (config / .env thresholds) are replayed day by day.
3. Entry = the OPEN of the first trading day after the filing day (what the live agent would get
   when it runs the next morning). Returns are measured at several horizons and compared to SPY
   over the identical dates.

Needs Alpaca keys for daily price bars (free IEX feed). Honest limitations, also printed at the end:
  * survivorship bias: delisted or renamed tickers return no bars and are skipped
  * IEX-only prices are not the consolidated tape
  * no commissions, slippage, taxes, or the exit rule; fixed-horizon returns only
  * the bulk datasets do not flag Rule 10b5-1 plans, but plan-based BUYS are rare
Usage:  python backtest.py --datasets 2025q1_form345.zip 2025q2_form345.zip
"""
import argparse
import csv
import io
import json
import os
import statistics
import time
import zipfile
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

from config import Config
from signals import select

DATA_BASE = "https://data.alpaca.markets"
HORIZONS = (5, 20, 60, 90)


# ---------- loading SEC datasets ----------

def _d(text):
    return datetime.strptime(text.strip().upper(), "%d-%b-%Y").date()


def _num(text):
    try:
        return float(str(text).replace(",", ""))
    except ValueError:
        return None


def _read_tsv(zf, wanted):
    names = {os.path.basename(n).upper(): n for n in zf.namelist()}
    key = wanted.upper() + ".TSV"
    if key not in names:
        raise SystemExit(f"{wanted}.tsv not found in zip (found: {sorted(names)})")
    with zf.open(names[key]) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"), delimiter="\t")


def load_dataset(path):
    """Return purchase rows (same shape as the live insider_buys table) from one quarterly zip."""
    with zipfile.ZipFile(path) as zf:
        subs = {}
        for r in _read_tsv(zf, "SUBMISSION"):
            if r["DOCUMENT_TYPE"].strip() != "4":
                continue
            try:
                subs[r["ACCESSION_NUMBER"]] = {
                    "filed": _d(r["FILING_DATE"]), "cik": r["ISSUERCIK"], "name": r["ISSUERNAME"],
                    "symbol": r["ISSUERTRADINGSYMBOL"].strip().upper()}
            except ValueError:
                continue
        owners = defaultdict(list)
        for r in _read_tsv(zf, "REPORTINGOWNER"):
            if r["ACCESSION_NUMBER"] in subs:
                owners[r["ACCESSION_NUMBER"]].append(r)

        rows = []
        for r in _read_tsv(zf, "NONDERIV_TRANS"):
            acc = r["ACCESSION_NUMBER"]
            if acc not in subs or r["TRANS_CODE"].strip().upper() != "P":
                continue
            if r["TRANS_ACQUIRED_DISP_CD"].strip().upper() != "A":
                continue
            if r.get("EQUITY_SWAP_INVOLVED", "").strip().upper() in ("1", "Y", "TRUE"):
                continue
            shares, price = _num(r["TRANS_SHARES"]), _num(r["TRANS_PRICEPERSHARE"])
            if not shares or not price or shares <= 0 or price <= 0:
                continue
            try:
                tx_date = _d(r["TRANS_DATE"])
            except ValueError:
                continue
            group = owners.get(acc) or []
            if not group:
                continue
            best = next((o for o in group if any(k in o["RPTOWNER_RELATIONSHIP"].upper()
                                                 for k in ("OFFICER", "DIRECTOR"))), group[0])
            rel = best["RPTOWNER_RELATIONSHIP"].upper()
            s = subs[acc]
            rows.append({
                "accession": acc, "issuer_cik": s["cik"], "symbol": s["symbol"], "issuer_name": s["name"],
                "owner_cik": best["RPTOWNERCIK"], "owner_name": best["RPTOWNERNAME"],
                "title": best.get("RPTOWNER_TITLE", "") or "",
                "is_officer": int("OFFICER" in rel), "is_director": int("DIRECTOR" in rel),
                "is_ten_pct": int("TENPERCENTOWNER" in rel),
                "tx_date": tx_date.isoformat(), "filed": s["filed"].isoformat(),
                "shares": shares, "price": price, "value": round(shares * price, 2), "tenb5": 0,
            })
        return rows


# ---------- price data ----------

class AlpacaBars:
    """Daily bars from Alpaca's market-data API with a small JSON cache."""

    def __init__(self, key_id, secret, cache_path="bars_cache.json", session=None, save_every=25):
        if not (key_id and secret):
            raise SystemExit("ALPACA_KEY_ID / ALPACA_SECRET_KEY are required for price history.")
        self.session = session or requests.Session()
        self.session.headers.update({"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret})
        self.cache_path = cache_path
        self.save_every, self._unsaved, self.fetched = save_every, 0, 0
        try:
            with open(cache_path) as fh:
                self.cache = json.load(fh)
        except (OSError, ValueError):
            self.cache = {}

    def get(self, symbol, start, end):
        key = f"{symbol}|{start}|{end}"
        if key not in self.cache:
            self.cache[key] = self._fetch(symbol, start, end)
            self.fetched += 1
            self._unsaved += 1
            if self.fetched % 25 == 0:
                print(f"   downloaded prices for {self.fetched} tickers so far...", flush=True)
            if self._unsaved >= self.save_every:
                self.save()
        return [(date.fromisoformat(d), o, c) for d, o, c in self.cache[key]]

    def save(self):
        """Write the cache to disk (atomically). Called every few downloads and once at the end."""
        if not self._unsaved:
            return
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.cache, fh)
        for _ in range(5):
            try:
                os.replace(tmp, self.cache_path)
                self._unsaved = 0
                return
            except PermissionError:    # OneDrive or antivirus can briefly lock the file on Windows
                time.sleep(1)
        print("   (could not update bars_cache.json just now; will retry)", flush=True)

    def _fetch(self, symbol, start, end):
        out, token = [], None
        while True:
            params = {"timeframe": "1Day", "start": start.isoformat(), "end": end.isoformat(),
                      "adjustment": "all", "feed": "iex", "limit": 10000}
            if token:
                params["page_token"] = token
            time.sleep(0.35)   # stay inside the free-tier request rate
            resp = self.session.get(f"{DATA_BASE}/v2/stocks/{symbol}/bars", params=params, timeout=60)
            if resp.status_code in (400, 404, 422):
                return []      # unknown / delisted symbol
            resp.raise_for_status()
            body = resp.json()
            out += [(b["t"][:10], b["o"], b["c"]) for b in body.get("bars") or []]
            token = body.get("next_page_token")
            if not token:
                return out


# ---------- replay ----------

def signal_events(rows, cfg, start, end):
    """Replay the live selection rules day by day. Returns [(signal_day, Signal)]."""
    by_filed = defaultdict(list)
    for r in rows:
        by_filed[r["filed"]].append(r)
    events, seen, last_signal = [], set(), {}
    day = start
    while day <= end:
        if day.weekday() < 5:
            window = []
            for back in range(cfg.window_days + 1):
                window += by_filed.get((day - timedelta(days=back)).isoformat(), [])
            held = {s for s, d in last_signal.items() if (day - d).days < cfg.hold_days}
            sigs, _ = select(window, cfg, day, seen, held)
            for s in sigs[:cfg.max_new_per_run]:
                events.append((day, s))
                seen.update(s.accessions)
                last_signal[s.ticker] = day
        day += timedelta(days=1)
    return events


def forward_returns(bars, bench, signal_day, horizons):
    """Return {horizon: (raw_return, excess_vs_benchmark)} entering at the next day's open."""
    dates = [b[0] for b in bars]
    i = bisect_right(dates, signal_day)
    if i >= len(bars):
        return {}
    entry_open = bars[i][1]
    bench_by_date = {b[0]: b for b in bench}
    entry = bench_by_date.get(dates[i])
    if not entry or not entry_open:
        return {}
    out = {}
    for h in horizons:
        if i + h >= len(bars):
            continue
        exit_date, exit_close = bars[i + h][0], bars[i + h][2]
        bench_exit = bench_by_date.get(exit_date)
        if not bench_exit:
            continue
        raw = exit_close / entry_open - 1
        out[h] = (raw, raw - (bench_exit[2] / entry[1] - 1))
    return out


def run_backtest(rows, cfg, bar_source, start, end, horizons=HORIZONS, benchmark="SPY"):
    events = signal_events(rows, cfg, start, end)
    pad_start, pad_end = start - timedelta(days=5), min(end + timedelta(days=200), date.today())
    bench = bar_source.get(benchmark, pad_start, pad_end)
    results, skipped = [], 0
    for day, sig in events:
        bars = bar_source.get(sig.ticker, pad_start, pad_end)
        rets = forward_returns(bars, bench, day, horizons) if bars else {}
        if not rets:
            skipped += 1
            continue
        results.append({"day": day, "ticker": sig.ticker, "why": sig.why, "returns": rets})
    summary = {}
    for h in horizons:
        pairs = [r["returns"][h] for r in results if h in r["returns"]]
        if not pairs:
            continue
        raw, exc = [p[0] for p in pairs], [p[1] for p in pairs]
        summary[h] = {"n": len(pairs), "mean_return": statistics.mean(raw),
                      "mean_excess": statistics.mean(exc), "median_excess": statistics.median(exc),
                      "beat_benchmark": sum(1 for e in exc if e > 0) / len(exc)}
    return {"signals": len(events), "skipped_no_prices": skipped, "events": results, "summary": summary}


def print_report(res, benchmark="SPY"):
    print(f"\nSignals generated: {res['signals']}   skipped (no price data): {res['skipped_no_prices']}")
    print(f"\n{'horizon':>8} {'n':>5} {'mean ret':>9} {'mean vs ' + benchmark:>12} {'median vs ' + benchmark:>14} {'% beating':>10}")
    for h, s in res["summary"].items():
        print(f"{str(h) + 'd':>8} {s['n']:>5} {s['mean_return']:>9.1%} {s['mean_excess']:>12.1%} "
              f"{s['median_excess']:>14.1%} {s['beat_benchmark']:>10.0%}")
    print("\nRead with care: survivorship bias (delisted tickers skipped), IEX-only prices, no costs or taxes,")
    print("overlapping holding periods, and a small sample can all flatter or distort these numbers.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datasets", nargs="+", required=True, help="quarterly SEC insider-transaction zips")
    ap.add_argument("--start", help="first signal day (YYYY-MM-DD); default = earliest filing + window")
    ap.add_argument("--end", help="last signal day (YYYY-MM-DD); default = latest filing")
    ap.add_argument("--benchmark", default="SPY",
                    help="ticker to compare against, e.g. SPY (large caps) or IWM (small caps)")
    ap.add_argument("--events-csv", default="backtest_events.csv")
    args = ap.parse_args()

    cfg = Config.from_env()
    rows = []
    for path in args.datasets:
        part = load_dataset(path)
        print(f"{path}: {len(part)} open-market purchase rows")
        rows += part
    if not rows:
        raise SystemExit("No purchase rows found.")
    filed = sorted(r["filed"] for r in rows)
    start = date.fromisoformat(args.start) if args.start else date.fromisoformat(filed[0]) + timedelta(days=cfg.window_days)
    end = date.fromisoformat(args.end) if args.end else date.fromisoformat(filed[-1])
    bars = AlpacaBars(cfg.alpaca_key_id, cfg.alpaca_secret)
    try:
        res = run_backtest(rows, cfg, bars, start, end, benchmark=args.benchmark)
    finally:
        bars.save()
    print_report(res, args.benchmark)
    with open(args.events_csv, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["signal_day", "ticker", "why"] + [f"ret_{h}d" for h in HORIZONS] + [f"excess_{h}d" for h in HORIZONS])
        for e in res["events"]:
            w.writerow([e["day"], e["ticker"], e["why"]]
                       + [f"{e['returns'][h][0]:.4f}" if h in e["returns"] else "" for h in HORIZONS]
                       + [f"{e['returns'][h][1]:.4f}" if h in e["returns"] else "" for h in HORIZONS])
    print(f"\nPer-signal results written to {args.events_csv}")


if __name__ == "__main__":
    main()

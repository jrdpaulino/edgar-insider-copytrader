"""Parameter sweep with a train/holdout split, built on backtest.py.

    python sweep.py --datasets 2026q1_form345.zip 2026q2_form345.zip 2026q3_form345.zip \
                    --split 2026-07-01 --benchmark IWM --horizon 90

Each variant tightens one or two signal rules. Results are split by signal date: "train" is before
--split, "holdout" is on/after it. Believe a variant only if it beats the benchmark in BOTH periods
and at more than one horizon. The "+/-mo" column is the standard error across calendar months,
which is a more honest error bar than counting every overlapping signal as independent.

Warning: every extra variant you test raises the odds that one looks good by luck alone.
"""
import argparse
import statistics
from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta

from backtest import AlpacaBars, load_dataset, run_backtest
from config import Config

VARIANTS = [
    ("baseline (.env settings)", {}),
    ("3+ insiders", {"min_insiders": 3}),
    ("cluster of $150k+", {"min_total_usd": 150_000}),
    ("3+ insiders and $150k+", {"min_insiders": 3, "min_total_usd": 150_000}),
    ("CEO/CFO alone, $250k+", {"min_insiders": 99}),
    ("CEO/CFO alone, $500k+", {"min_insiders": 99, "single_buy_usd": 500_000}),
    ("price $10 or more", {"min_price": 10.0}),
    ("trade within 5 days", {"max_tx_age_days": 5}),
]


def summarize(events, horizon):
    """Stats of excess return at one horizon, or None if there are no usable events."""
    pts = [(e["day"], e["returns"][horizon][1]) for e in events if horizon in e["returns"]]
    if not pts:
        return None
    excess = [x for _, x in pts]
    by_month = defaultdict(list)
    for day, x in pts:
        by_month[(day.year, day.month)].append(x)
    month_means = [statistics.mean(v) for v in by_month.values()]
    se = statistics.stdev(month_means) / len(month_means) ** 0.5 if len(month_means) > 1 else float("nan")
    return {"n": len(excess), "mean": statistics.mean(excess), "median": statistics.median(excess),
            "beat": sum(1 for x in excess if x > 0) / len(excess), "se": se, "months": len(month_means)}


def run_sweep(rows, cfg, bars, start, end, split, horizon, benchmark="SPY", variants=VARIANTS):
    out = []
    for name, overrides in variants:
        res = run_backtest(rows, replace(cfg, **overrides), bars, start, end,
                           horizons=(horizon,), benchmark=benchmark)
        train = [e for e in res["events"] if e["day"] < split]
        hold = [e for e in res["events"] if e["day"] >= split]
        out.append({"name": name, "signals": res["signals"],
                    "train": summarize(train, horizon), "holdout": summarize(hold, horizon)})
    return out


def _fmt(s):
    if not s:
        return f"{'-':>4} {'-':>7} {'-':>6} {'-':>7} {'-':>5}"
    se = "n/a" if s["se"] != s["se"] else f"{s['se'] * 100:.1f}%"
    return f"{s['n']:>4} {s['mean']:>+7.1%} {se:>6} {s['median']:>+7.1%} {s['beat']:>5.0%}"


def format_table(results, split, horizon, benchmark="SPY"):
    cols = f"{'n':>4} {'mean':>7} {'+/-mo':>6} {'median':>7} {'beat':>5}"
    lines = [
        f"Excess return vs {benchmark} at {horizon} trading days (entry at next day's open)",
        f"{'':<26} {'':>5} | {'TRAIN: signals before ' + str(split):<31} | HOLDOUT: from {split}",
        f"{'variant':<26} {'sigs':>5} | {cols} | {cols}",
        "-" * 92,
    ]
    for r in results:
        lines.append(f"{r['name']:<26} {r['signals']:>5} | {_fmt(r['train'])} | {_fmt(r['holdout'])}")
    lines += [
        "",
        f"{len(results)} variants were tested. By chance alone, about 1 in 20 would look 'significant'.",
        "Trust a variant only if mean and median are positive in BOTH train and holdout, and the mean",
        "is larger than about twice its +/-mo error bar. Otherwise treat it as noise.",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datasets", nargs="+", required=True, help="quarterly SEC insider-transaction zips")
    ap.add_argument("--split", required=True, help="first holdout signal day (YYYY-MM-DD)")
    ap.add_argument("--benchmark", default="SPY", help="ticker to compare against (SPY, IWM, ...)")
    ap.add_argument("--horizon", type=int, default=90, help="holding period in trading days (default 90)")
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
    start = date.fromisoformat(filed[0]) + timedelta(days=cfg.window_days)
    end = date.fromisoformat(filed[-1])
    split = date.fromisoformat(args.split)
    if not start < split <= end:
        raise SystemExit(f"--split must fall between {start} and {end}.")

    bars = AlpacaBars(cfg.alpaca_key_id, cfg.alpaca_secret)
    print("Running variants (price history is cached, so only the first run is slow)...")
    try:
        results = run_sweep(rows, cfg, bars, start, end, split, args.horizon, args.benchmark)
    finally:
        bars.save()
    print()
    print(format_table(results, split, args.horizon, args.benchmark))


if __name__ == "__main__":
    main()

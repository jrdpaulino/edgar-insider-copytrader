"""Multi-signal walk-forward research CLI (backtest only; never places orders).

    python multisignal.py download --years 5          # bars for the tradable universe (slow, resumable)
    python multisignal.py news --years 3              # news counts per ticker per day (needs news access)
    python multisignal.py backtest --split 0.7        # compare adaptive vs equal-weight vs each signal
    python multisignal.py backtest --signals momentum,trend,high52,news
"""
import argparse
import os
from datetime import date, timedelta

from config import load_dotenv
from multi.alpaca_data import AccessDenied, DataClient, download_bars, download_news
from multi.engine import ALL_SIGNALS, CLASSIC_SIGNALS, NEW_PRICE_SIGNALS, Params, build_panel, format_report, rows_to_csv
from multi.store import PanelStore


def client_from_env():
    load_dotenv()
    return DataClient(os.environ.get("ALPACA_KEY_ID", ""), os.environ.get("ALPACA_SECRET_KEY", ""),
                      paper=os.environ.get("ALPACA_PAPER", "true").lower() != "false")


def cmd_download(a):
    store, client = PanelStore(a.db), client_from_env()
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=int(a.years * 365.25))
    symbols = client.tradable_symbols()
    if a.limit:
        symbols = symbols[:a.limit]
    print(f"{len(symbols)} candidate tickers; downloading bars {start} -> {end} (resumable; Ctrl+C is safe)")
    download_bars(client, store, symbols, start, end)
    print(f"{len(store.kept_symbols())} tickers kept after liquidity/history screening")


def cmd_news(a):
    store, client = PanelStore(a.db), client_from_env()
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=int(a.years * 365.25))
    try:
        download_news(client, store, start, end)
    except AccessDenied as exc:
        raise SystemExit(f"News access refused: {exc}\nThe news signal needs historical news on your Alpaca plan.")


def cmd_backtest(a):
    store = PanelStore(a.db)
    presets = {"classic": CLASSIC_SIGNALS, "new": NEW_PRICE_SIGNALS, "price": CLASSIC_SIGNALS + NEW_PRICE_SIGNALS}
    signals = presets.get(a.signals) or tuple(s.strip() for s in a.signals.split(",") if s.strip())
    bad = [s for s in signals if s not in ALL_SIGNALS]
    if bad:
        raise SystemExit(f"Unknown signals {bad}; choose from {ALL_SIGNALS}")
    params = Params(horizon=a.horizon, top_n=a.top_n, shrink=a.shrink, cost_bps=a.cost_bps,
                    min_dollar_volume=a.min_dollar_volume, signals=signals)
    news = store.load_news() if "news" in signals else None
    if "news" in signals and not news:
        raise SystemExit("No news data stored. Run `news` first, or drop news from --signals.")
    panel = build_panel(store, a.benchmark, params, news)
    print(f"{len(panel.syms)} stocks, {len(panel.days)} trading days ({panel.days[0]} -> {panel.days[-1]})")
    rows = panel.run()
    if not rows:
        raise SystemExit("No evaluable periods; download more history (--years 4 or more).")
    eval_start = a.eval_start
    if not eval_start and a.split:
        days = sorted({r["day"] for r in rows})
        eval_start = days[int(len(days) * a.split)]
    print()
    print(format_report(rows, params, a.benchmark, eval_start))
    if a.csv:
        rows_to_csv(rows, a.csv)
        print(f"\nPer-period rows written to {a.csv}")


def cmd_status(a):
    store = PanelStore(a.db)
    st = store.statuses()
    print(f"symbols: {sum(v == 'kept' for v in st.values())} kept of {len(st)} screened")
    rng = store.news_range()
    print(f"news days downloaded: {rng[0]} -> {rng[1]}" if rng else "news: nothing downloaded")
    if rng:
        rows = store.db.execute("SELECT substr(day,1,7), SUM(articles) FROM news_days GROUP BY 1 ORDER BY 1").fetchall()
        print("articles seen per month (before the <=5-ticker filter):")
        for month, total in rows:
            print(f"  {month}  {int(total or 0):>8}")
        syms, n = store.db.execute("SELECT COUNT(DISTINCT symbol), COUNT(*) FROM news_counts").fetchone()
        print(f"tickers with news: {syms}; (day, ticker) rows: {n}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="panel.sqlite")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--years", type=float, default=5)
    d.add_argument("--limit", type=int, default=0, help="only the first N tickers (for a quick trial)")
    d.set_defaults(fn=cmd_download)
    n = sub.add_parser("news")
    n.add_argument("--years", type=float, default=3)
    n.set_defaults(fn=cmd_news)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    b = sub.add_parser("backtest")
    b.add_argument("--benchmark", default="IWM")
    b.add_argument("--signals", default="classic",
                   help="classic | new | price (all seven) | comma list from: " + ",".join(ALL_SIGNALS))
    b.add_argument("--top-n", type=int, default=30)
    b.add_argument("--horizon", type=int, default=20)
    b.add_argument("--split", type=float, default=0.0, help="fraction of periods used as train, rest holdout")
    b.add_argument("--eval-start", default="", help="explicit holdout start date, YYYY-MM-DD")
    b.add_argument("--cost-bps", type=float, default=10.0)
    b.add_argument("--shrink", type=float, default=0.5)
    b.add_argument("--min-dollar-volume", type=float, default=300_000.0)
    b.add_argument("--csv", default="")
    b.set_defaults(fn=cmd_backtest)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()

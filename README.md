# EDGAR Insider-Cluster Copy-Trader

A Python agent that reads SEC Form 4 filings each trading day, finds **clusters of open-market
insider purchases** (several officers/directors of one company buying within a short window, or one
large CEO/CFO purchase), and buys small positions through Alpaca behind a hard risk gate. Positions
are closed automatically after a hold period.

> Not financial advice. Insider-buying edges are modest, shrink as they become known, and can
> disappear. Backtest first, run on paper for weeks, and never trade money you can't lose.

## Flow

```
EDGAR daily index -> edgar.py (parse Form 4 XML) -> SQLite -> signals.py (clusters)
   -> exits (hold period) -> risk.py (limits, no-chase check) -> broker.py (Alpaca) -> audit log
```

| File | Job |
|---|---|
| `edgar.py` | Polite EDGAR client; lists Form 4s from the daily index; extracts code-`P` purchases |
| `signals.py` | Cluster rules and rejection reasons |
| `risk.py` | Per-order, daily, per-position and open-position limits; don't-chase price check |
| `broker.py` | Alpaca REST client (paper or live), including last-trade price |
| `state.py` | Stores filings, purchases, handled signals, orders and open lots |
| `agent.py` | The daily run |
| `backtest.py` | Replays the same rules on SEC's historical datasets |
| `test_*.py` | 76 offline tests (fake broker, canned EDGAR responses) |

## What counts as a signal

Kept: Form 4 transaction code **P** (open-market/private purchase, acquired) by an **officer or
director**. Ignored: sales, grants, option exercises, tax withholding, gifts, equity swaps, purchases
flagged as Rule 10b5-1 plans, 10%-owner-only filers, stocks under `MIN_PRICE`, trades older than
`MAX_TX_AGE_DAYS`.

A company qualifies when, within `WINDOW_DAYS` (by filing date), either
`MIN_INSIDERS` different insiders bought at least `MIN_TOTAL_USD` combined, or a CEO/CFO alone bought
at least `SINGLE_BUY_USD`. All thresholds are in `.env`.

## Quick setup (recommended)

```
python3 setup_local.py              # macOS/Linux. On Windows use: py setup_local.py
python3 setup_local.py --schedule   # same, plus a weekday schedule (cron, or Task Scheduler on Windows)
```

It never enables trading: `.env` keeps `PAPER=true` and `DRY_RUN=true`, and you add your own keys.
It needs Python 3.9+ first. On Windows: `winget install Python.Python.3.12`, then open a **new** terminal
(the `python` shortcut from the Microsoft Store is only a placeholder; use `py`). The manual steps below do the same thing.

## Setup

1. `pip install -r requirements.txt`
2. Create an Alpaca **paper** account and copy its API keys.
3. `cp .env.example .env`. Set `SEC_USER_AGENT` to your real name and email (the SEC requires it and
   blocks anonymous clients), plus the Alpaca keys.
4. `python -m unittest` (offline; all should pass).
5. `python agent.py --collect-only`: downloads the last `FILING_LOOKBACK_DAYS` of filings and trades
   nothing. **The first run is slow** (roughly 1,000+ filings per business day at ~6 requests/second,
   so expect 30-60 minutes). Later runs only fetch new filings.
6. `python agent.py --ignore-market-hours`: a **dry run**; logs what it would do.

## Backtest before trusting it

1. From the SEC's "Insider Transactions Data Sets" page, download a few quarterly zips
   (e.g. `2025q1_form345.zip`).
2. `python backtest.py --datasets 2025q1_form345.zip 2025q2_form345.zip`

It replays your configured rules day by day, enters at the next trading day's open, and reports
forward returns at 5/20/60/90 trading days versus SPY, plus a per-signal CSV. Known limits: survivorship
bias (delisted tickers have no price data and are skipped), IEX-only prices, no costs or taxes, and
overlapping holds. Treat a good result as "worth paper trading", not as proof.

## Does it beat the market? Benchmarks and the holdout sweep

SPY is dominated by large caps, while insider-cluster buys skew small, so also compare against a
small-cap index:

```
python backtest.py --datasets 2026q1_form345.zip 2026q2_form345.zip --benchmark IWM
```

`sweep.py` tests eight tighter versions of the signal and splits the results by date, so a variant
that only worked in one stretch can't pass as a discovery:

```
python sweep.py --datasets 2026q1_form345.zip 2026q2_form345.zip 2026q3_form345.zip --split 2026-07-01 --benchmark IWM
```

Trust a variant only if it beats the benchmark in both the train and holdout columns, with the mean
well above its `+/-mo` error bar. More history (older quarterly zips from the same SEC page) makes
every conclusion stronger.

## Going live, in order

1. Dry run for a couple of weeks; read `agent.log`. (Dry runs don't consume signals, so the same
   candidates reappear each day until you go to paper.)
2. Paper trading: `DRY_RUN=false`, keep `PAPER=true`.
3. Live: `PAPER=false`, `DRY_RUN=false`, live keys, and `CONFIRM_LIVE_TRADING=I_UNDERSTAND_THE_RISKS`.
   Start with a tiny `MAX_ORDER_USD`.

Create a file named `STOP` next to `agent.py` to halt all trading (buys and exits) immediately.

## Scheduling (daily, automatic)

Run on weekdays shortly after the open; the agent checks the market clock itself, and EDGAR's index
for a day is generally complete by the next morning, so this picks up yesterday's filings.

```
CRON_TZ=America/New_York
45 9 * * 1-5  cd /path/to/project && /usr/bin/python3 agent.py >> cron.log 2>&1
```

Alternatives: a systemd timer, or a scheduled GitHub Actions workflow (store keys as secrets and persist
`edgar_state.db` between runs, or dedupe and lots are lost).

## Behavior to know about

- **Flat sizing.** Every new position is `MAX_ORDER_USD`, bought as a fractional dollar order.
- **Don't chase.** If the stock is more than `MAX_PREMIUM_PCT` above the insiders' average price, the signal
  is deferred and re-checked on later runs while it is still inside the window.
- **Only its own positions.** The agent refuses to buy a ticker already in the account and only
  closes lots it opened itself. Don't manually trade a ticker the agent holds.
- **Time-based exit.** Positions close `HOLD_DAYS` after entry. There's no stop-loss.
- **Idempotent.** Filings and signals are tracked in SQLite and each order has a per-day
  `client_order_id`, so re-runs won't double-buy.
- **Transient vs permanent.** Market closed, daily cap, low buying power, missing price, price run-up and
  the per-run cap are retried; untradable, already held, or over the position cap are rejected for good.

## Data notes

- Form 4 is due within 2 business days of the trade, but you're still behind the insider: the stock may
  have moved before the filing, which is why the don't-chase check exists.
- Filings are fetched from `www.sec.gov/Archives` using EDGAR's daily form index. Stay under the SEC's
  10 requests/second limit (the default delay keeps you near 6/s).
- Parsing follows the SEC's published ownership XML schema and was tested on synthetic filings. It has
  not been run against live EDGAR from the build environment, so watch the first collection run's log
  (`unparseable` and `failed` counts) before relying on it.

## Other brokers

`broker.py` is a thin interface: `account`, `positions`, `asset`, `clock`, `latest_price`,
`market_order`, `close_position`. Implement those for Interactive Brokers, Schwab or Tradier.
Fidelity has no public trading API for individual customers.


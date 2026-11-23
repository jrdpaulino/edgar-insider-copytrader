# Progress over time

This project began as a trading agent and became a research programme about whether it should trade at all. The
commit history follows the same order as the stages below.

## Before this: the Congress copy-trader

The first version copied trades that members of Congress disclose. It is kept in
[congress-copytrader](https://github.com/jrdpaulino/congress-copytrader). The limit was the data: disclosures can arrive
up to 45 days after a trade, so a daily report is rarely news. That pushed the project to Form 4 insider filings, which
are due within two business days.

## Stage 1: collect, signal, trade behind a gate

An agent that reads the SEC's daily Form 4 index, parses the ownership XML and keeps open-market purchases by
officers and directors. A company becomes a signal when several insiders buy within a short window, or one CEO or
CFO makes a large purchase. Orders go through a risk gate and an Alpaca client.

What was built deliberately cautiously:
- Dry run and paper trading are the defaults. Live trading needs two switches and a typed confirmation.
- Per-order, daily, per-position and open-position limits, plus a "don't chase" check against the insiders' price.
- Idempotent runs: filings and signals are tracked in SQLite and each order has a per-day client order id.
- A `STOP` file halts all trading immediately.
- Offline tests with a fake broker and canned EDGAR responses (42 at this stage, 76 by the end).

## Stage 2: backtest the rule before trusting it

`backtest.py` replays the same rules on the SEC's historical insider datasets, entering at the next day's open and
measuring forward returns at 5, 20, 60 and 90 trading days against SPY and a small-cap benchmark (IWM).
`sweep.py` then tests eight tighter variants of the signal, split by date into a training and a holdout period, so a
variant that worked in only one stretch cannot pass as a discovery.

Known limits, stated in the README at the time: survivorship bias (delisted tickers have no price data), IEX-only
prices, no costs or taxes, and overlapping holds.

## Stage 3: do several signals beat one?

`multisignal.py` and the `multi/` package test whether a handful of signals, each weighted by how well it has
recently predicted returns, beat the best single signal. Price history for about 2,800 stocks over five years was
downloaded, and the signals were:

- Batch one: 12-1 month momentum, 200-day trend, 52-week-high proximity, and news buzz.
- Batch two, hypotheses fixed before running: reversal, low volatility, beta-adjusted momentum and volume spikes.

Rebalancing was monthly on non-overlapping 20-trading-day periods. Weights came only from correlations already known
at each rebalance date, with negatives set to zero and the result shrunk toward equal weights. Results were judged on
a holdout block, and the README says to count attempts, since every extra idea raises the chance that one looks good
by luck.

## Results

Figures are from the project's own runs in November 2026. Active return is the stock's return minus the benchmark's,
measured from the next day's open after the filing.

### Stage 2: insider-cluster signal

| Test | Benchmark | Horizon | Mean | Median | Beat benchmark |
| --- | --- | --- | ---: | ---: | ---: |
| All 2,104 signals | SPY | 90 days | -2.2% | -4.5% | 41% |
| All 2,104 signals | IWM | 90 days | -1.8% | -3.4% | 43% |
| Baseline, train 2023-2025 (n=1,387) | IWM | 90 days | -1.4% +/- 0.7% | negative | n/a |
| Baseline, holdout 2026 (n=422) | IWM | 90 days | -3.2% +/- 4.2% | negative | n/a |

All eight tested variants (3+ insiders, $150k+ clusters, CEO/CFO alone, price $10+, trade within 5 days, and
combinations) had a negative mean and median in both periods. The +/- is one standard error, clustered by month.
Because eight variants were compared on the same data, a lucky one would have been expected by chance, and none was.

A live dry run on 2026-11-19 collected 5,871 filings with no failures and none unparseable, found 67 signals and
produced 4 simulated orders (55 deferred, 8 rejected). Nothing reached the broker.

### Stage 3: price signals and the adaptive model

2,846 stocks and five years of prices. Mean active return per 20-trading-day period against IWM, +/- one standard
error. The portfolio held the top 30 stocks at equal weight with 10 bps cost per side.

| Strategy | Train (n=32) | Holdout, Oct 2025 on (n=14) |
| --- | ---: | ---: |
| Adaptive | +0.14% +/- 1.0% | -2.90% +/- 1.3% |
| Equal weight | +1.06% +/- 1.0% | -3.13% +/- 1.6% |
| Momentum only | +2.12% +/- 1.5% | -1.92% +/- 2.4% |
| Trend only | -0.55% +/- 1.2% | -2.53% +/- 1.8% |
| 52-week high only | -0.59% +/- 0.8% | -2.02% +/- 1.5% |

The pass test was that the adaptive model must beat its best single signal on holdout data by more than the standard
error. It failed: it trailed momentum in the holdout and trailed equal weight in training. Momentum looked promising in
training (about 1.4 standard errors) and did not carry into the holdout.

The news-buzz signal could not be tested. The available news history starts in April 2026, which leaves about seven
months and only three scorable periods, so that run says nothing about the signal.

### Caveats

- The stock list is today's survivors, which flatters every number.
- Prices and volume are IEX-only.
- Backtest holds overlap in the insider-signal tests.
- All 14 holdout periods fall in one market regime.
- Order placement was never tested against a live broker.
- The first version of the adaptive engine let a signal with too little history block all the others. This is fixed
  in the code: a signal with fewer than 12 readings now sits out at weight 0.

## Outcome

None of the three stages showed an edge worth trading. The recommendation at the time was to keep the agent in dry run
and not trade it with money.

## What it taught

1. **Measure before you build the trading part.** The order-placing code was written first and turned out to be the
   least important piece.
2. **Holdouts and attempt counts are the real work.** A sweep without a holdout period manufactures discoveries.
3. **Know your biases.** Survivorship in the stock list and IEX-only volume both flatter results.
4. **A negative result is a result.** Stopping was the correct call.

## What came next

[Market Signal Conductor](https://github.com/jrdpaulino/market-signal-conductor) starts from those lessons: the track
record of the model's own forecasts is the product, signals earn influence from outcomes already known on the day, and
the criteria for trading are written as code in a gate that stays closed until the evidence is there.

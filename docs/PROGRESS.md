# Progress over time

This project began as a trading agent and became a research programme about whether it should trade at all. The
commit history follows the same order as the stages below.

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

## Outcome

None of the three stages showed an edge worth trading. The result files from these runs were not kept in this
repository, so the figures are not reproduced here. The commands in the README rerun everything from the SEC and
Alpaca data.

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

"""Daily EDGAR insider-cluster copy-trading agent.

Run once per trading day, shortly after the open (see README for cron). Each run:
  collect new Form 4 filings -> close positions past their hold period -> build cluster signals
  -> risk-check -> place (or simulate) orders -> record everything in SQLite.
"""
import argparse
import json
import logging
import os
from datetime import datetime, timedelta

from broker import Alpaca, BrokerError
from config import Config
from edgar import EdgarClient, collect
from risk import RiskGate
from signals import select, summarize_rejections
from state import State

log = logging.getLogger("agent")


def today_eastern():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:  # tzdata missing
        return datetime.now().date()


def run_exits(cfg, broker, state, today):
    """Close positions this agent opened once they are older than HOLD_DAYS."""
    closed = 0
    lots = state.open_lots()
    if not lots:
        return closed
    positions = broker.positions()
    for lot in lots:
        age = (today - lot.entry_day).days
        if age < cfg.hold_days:
            continue
        if lot.symbol not in positions:
            log.info("EXIT   %s: position no longer in the account; closing the lot", lot.symbol)
            if not cfg.dry_run:
                state.close_lot(lot.symbol, today, "position not found")
            continue
        if cfg.dry_run:
            log.info("[DRY RUN] would close %s (held %d days)", lot.symbol, age)
            continue
        try:
            broker.close_position(lot.symbol)
        except BrokerError as exc:
            log.error("EXIT FAILED %s: %s", lot.symbol, exc)
            continue
        state.close_lot(lot.symbol, today, f"held {age} days")
        state.log_order(today, lot.symbol, "sell", lot.notional, "", "submitted", False, f"hold period {age}d")
        log.info("EXIT   %s closed after %d days", lot.symbol, age)
        closed += 1
    return closed


def run_once(cfg, collector, broker, state, today=None, ignore_market_hours=False, collect_only=False):
    today = today or today_eastern()
    summary = {"date": str(today), "dry_run": cfg.dry_run, "paper": cfg.paper,
               "signals": 0, "orders": 0, "deferred": 0, "rejected": 0, "exits": 0}

    if os.path.exists(cfg.kill_switch_path):
        log.warning("Kill switch file %r present - halting.", cfg.kill_switch_path)
        summary["halted"] = True
        return summary

    summary["collected"] = collector(state, today)
    if collect_only:
        return summary

    market_open = ignore_market_hours or bool(broker.clock().get("is_open"))
    if market_open:
        summary["exits"] = run_exits(cfg, broker, state, today)

    buys = state.buys_since(today - timedelta(days=cfg.window_days))
    signals, rejected = select(buys, cfg, today, state.seen_ids(), state.open_lot_symbols())
    log.info("%d purchase rows -> %d signals. Filtered out: %s",
             len(buys), len(signals), summarize_rejections(rejected) or "none")
    summary["signals"] = len(signals)
    if not signals:
        return summary
    if not market_open:
        log.info("Market is closed; signals stay pending for the next run.")
        summary["market_closed"] = True
        return summary

    gate = RiskGate(cfg, broker, state, today)
    snap = gate.snapshot()
    placed = 0

    for sig in signals:
        tag = f"BUY {sig.ticker} ({sig.why}: {', '.join(sig.insiders)})"
        if placed >= cfg.max_new_per_run:
            log.info("DEFER  %s: max %d new positions per run", tag, cfg.max_new_per_run)
            summary["deferred"] += 1
            continue
        dec = gate.check(sig, snap)
        if dec.verdict == "retry":
            log.info("DEFER  %s: %s", tag, dec.reason)
            summary["deferred"] += 1
            continue
        if dec.verdict == "reject":
            log.info("REJECT %s: %s", tag, dec.reason)
            summary["rejected"] += 1
            if not cfg.dry_run:
                state.mark(sig.accessions, "rejected", dec.reason)
            continue

        coid = f"edg-{today:%Y%m%d}-{sig.ticker}"
        broker_id, status = "", "dry_run"
        if cfg.dry_run:
            log.info("[DRY RUN] would %s for $%.2f", tag, dec.notional)
        else:
            try:
                order = broker.market_order(sig.ticker, "buy", dec.notional, coid)
            except BrokerError as exc:
                log.error("FAILED %s: %s", tag, exc)
                continue
            broker_id = str(order.get("id", "")) if isinstance(order, dict) else ""
            status = "submitted"
            log.info("ORDER  %s $%.2f -> %s", tag, dec.notional, broker_id)
            state.mark(sig.accessions, "submitted", broker_id)
            state.open_lot(sig.ticker, today, dec.notional, broker_id)

        state.log_order(today, sig.ticker, "buy", dec.notional, broker_id, status, cfg.dry_run, sig.why)
        gate.apply(snap, sig, dec)
        placed += 1
        summary["orders"] += 1
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ignore-market-hours", action="store_true",
                    help="skip the market-open check (useful for dry runs)")
    ap.add_argument("--collect-only", action="store_true",
                    help="only download and store filings (primes the database), place nothing")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("agent.log", encoding="utf-8")])
    cfg = Config.from_env()
    mode = "DRY RUN" if cfg.dry_run else ("PAPER" if cfg.paper else "LIVE")
    log.info("Starting in %s mode", mode)

    edgar = EdgarClient(cfg.sec_user_agent, delay=cfg.request_delay)
    collector = lambda st, td: collect(edgar, st, td, cfg.filing_lookback_days)  # noqa: E731
    broker = Alpaca(cfg.alpaca_key_id, cfg.alpaca_secret, paper=cfg.paper)
    summary = run_once(cfg, collector, broker, State(cfg.db_path),
                       ignore_market_hours=args.ignore_market_hours, collect_only=args.collect_only)
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()

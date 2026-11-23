"""Pre-trade risk gate for new buys. Every buy must pass here; the agent cannot bypass it.

Verdicts:
  ok      -> place the order
  retry   -> transient problem (daily cap, buying power, price ran up); re-evaluated next run
  reject  -> permanent problem (not tradable, already held, would breach position cap)
"""
import logging
from dataclasses import dataclass

from broker import BrokerError

log = logging.getLogger(__name__)


@dataclass
class Decision:
    verdict: str            # ok | retry | reject
    reason: str = ""
    notional: float = 0.0


class RiskGate:
    def __init__(self, cfg, broker, state, today):
        self.cfg, self.broker, self.state, self.today = cfg, broker, state, today

    def snapshot(self):
        acct = self.broker.account()
        return {
            "equity": float(acct["equity"]),
            "buying_power": float(acct["buying_power"]),
            "blocked": bool(acct.get("trading_blocked") or acct.get("account_blocked")),
            "positions": self.broker.positions(),
            "spent": self.state.spent_today(self.today, self.cfg.dry_run),
        }

    def check(self, sig, snap):
        cfg = self.cfg
        if snap["blocked"]:
            return Decision("retry", "account is blocked from trading")

        asset = self.broker.asset(sig.ticker)
        if not asset or asset.get("status") != "active" or not asset.get("tradable"):
            return Decision("reject", "asset not tradable at this broker")
        if not asset.get("fractionable"):
            return Decision("reject", "not fractionable (dollar-amount orders unsupported)")

        # Never touch a ticker that is already in the account: the agent only closes positions it opened.
        if snap["positions"].get(sig.ticker, 0.0) > 0:
            return Decision("reject", "ticker already held in the account")

        notional = sig.notional_usd
        if snap["equity"] <= 0:
            return Decision("retry", "no equity")
        if snap["spent"] + notional > cfg.max_daily_usd:
            return Decision("retry", f"daily cap ${cfg.max_daily_usd:,.0f} reached")
        if notional > snap["buying_power"]:
            return Decision("retry", "insufficient buying power")
        open_count = sum(1 for v in snap["positions"].values() if v > 0)
        if open_count >= cfg.max_open_positions:
            return Decision("retry", "max open positions reached")
        if notional / snap["equity"] > cfg.max_position_pct:
            return Decision("reject", f"would exceed {cfg.max_position_pct:.0%} position cap")

        # Don't chase: the insiders paid avg_price; skip while the stock has run well past it.
        try:
            price = self.broker.latest_price(sig.ticker)
        except BrokerError as exc:
            return Decision("retry", f"no current price ({exc})")
        limit = sig.avg_price * (1 + cfg.max_premium_pct)
        if price > limit:
            pct = (price / sig.avg_price - 1) * 100
            return Decision("retry", f"price ${price:.2f} is {pct:.0f}% above insiders' average ${sig.avg_price:.2f}")
        return Decision("ok", notional=notional)

    @staticmethod
    def apply(snap, sig, dec):
        """Update the in-memory snapshot after an order so later checks see it."""
        snap["spent"] += dec.notional
        snap["buying_power"] -= dec.notional
        snap["positions"][sig.ticker] = snap["positions"].get(sig.ticker, 0.0) + dec.notional

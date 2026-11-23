"""Turn stored insider purchases into trade signals.

The signal is *cluster buying*: several different officers/directors of the same company making
open-market purchases within a short window, or a single large CEO/CFO purchase. Every rejected
row gets a reason so a dry run explains itself.
"""
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date

TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
CEO_CFO = re.compile(r"\b(ceo|cfo|chief executive|chief financial)\b|^president\b", re.I)


@dataclass(frozen=True)
class Signal:
    ticker: str
    issuer_name: str
    insiders: tuple
    n_insiders: int
    total_usd: float
    avg_price: float          # shares-weighted average price the insiders paid
    accessions: tuple
    notional_usd: float       # how much WE buy
    why: str


def _d(value):
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def reject_reason(r, cfg, today):
    """Return None if the purchase row is usable, else a short reason."""
    sym = r.get("symbol", "")
    if not TICKER_RE.match(sym):
        return "invalid ticker"
    if sym in cfg.ticker_blocklist:
        return "ticker blocklisted"
    if r.get("tenb5"):
        return "10b5-1 plan"
    if not (r.get("is_officer") or r.get("is_director")):
        return "not an officer or director"
    if not r.get("price") or r["price"] < cfg.min_price:
        return "below minimum price"
    if not r.get("value") or r["value"] <= 0:
        return "no value"
    tx, filed = _d(r["tx_date"]), _d(r["filed"])
    if filed > today or tx > today:
        return "dated in the future"
    if (today - filed).days > cfg.window_days:
        return "outside window"
    if (today - tx).days > cfg.max_tx_age_days:
        return "trade too old"
    return None


def select(rows, cfg, today, seen_ids=frozenset(), held=frozenset()):
    """Return (signals, rejected) where rejected is a list of (row_or_symbol, reason)."""
    rejected = []
    groups = defaultdict(list)
    for r in rows:
        reason = reject_reason(r, cfg, today)
        if reason:
            rejected.append((r, reason))
        else:
            groups[r["symbol"]].append(r)

    signals = []
    for sym, items in groups.items():
        accessions = tuple(sorted({r["accession"] for r in items}))
        if sym in held:
            rejected.append((sym, "already holding"))
            continue
        if all(a in seen_ids for a in accessions):
            rejected.append((sym, "already processed"))
            continue
        insiders = {}
        for r in items:
            insiders[r.get("owner_cik") or r["owner_name"]] = r["owner_name"]
        total = sum(r["value"] for r in items)
        shares = sum(r["shares"] for r in items)
        avg_price = total / shares if shares else 0.0
        ceo = any(CEO_CFO.search(r.get("title") or "") for r in items)
        n = len(insiders)

        if n >= cfg.min_insiders and total >= cfg.min_total_usd:
            why = f"{n} insiders bought ${total:,.0f}"
        elif ceo and total >= cfg.single_buy_usd:
            why = f"CEO/CFO bought ${total:,.0f}"
        else:
            rejected.append((sym, "below cluster threshold"))
            continue
        signals.append(Signal(
            ticker=sym,
            issuer_name=items[0].get("issuer_name", ""),
            insiders=tuple(sorted(insiders.values())),
            n_insiders=n,
            total_usd=total,
            avg_price=avg_price,
            accessions=accessions,
            notional_usd=cfg.max_order_usd,
            why=why,
        ))
    signals.sort(key=lambda s: (-s.n_insiders, -s.total_usd, s.ticker))
    return signals, rejected


def summarize_rejections(rejected):
    return dict(Counter(reason for _, reason in rejected))

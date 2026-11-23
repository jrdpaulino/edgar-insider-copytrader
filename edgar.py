"""SEC EDGAR collector: finds Form 4 filings and extracts open-market insider purchases.

Flow per run:
  1. For each recent business day, download EDGAR's daily form index and list Form 4 filings.
  2. Download each filing not seen before, parse the ownership XML, and store purchases.

SEC fair-access rules: send a User-Agent that identifies you (name + email) and stay under
10 requests/second. This client enforces a delay between requests and backs off on errors.

Only transaction code "P" (open-market or private purchase, acquired) is kept. Sales, grants,
option exercises, tax withholding and gifts are ignored, as are purchases flagged as made
under a Rule 10b5-1 plan.
"""
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

ARCHIVES = "https://www.sec.gov/Archives/"
INDEX_ROW = re.compile(r"^(4)\s{2,}(.+?)\s+(\d{1,10})\s+(\d{8})\s+(edgar/\S+)\s*$")
OWNERSHIP_DOC = re.compile(r"<ownershipDocument>.*?</ownershipDocument>", re.S)
ACCESSION = re.compile(r"(\d{10}-\d{2}-\d{6})")
PLAN_NOTE = re.compile(r"10b5\W?1", re.I)
TRUE = {"1", "true", "y", "yes"}


class EdgarError(RuntimeError):
    pass


@dataclass(frozen=True)
class IndexEntry:
    accession: str
    filed: date
    path: str


def daily_index_url(day):
    quarter = (day.month - 1) // 3 + 1
    return f"{ARCHIVES}edgar/daily-index/{day.year}/QTR{quarter}/form.{day:%Y%m%d}.idx"


def parse_index(text):
    """Return one IndexEntry per Form 4 accession (EDGAR lists each filing once per party)."""
    seen, out = set(), []
    for line in text.splitlines():
        m = INDEX_ROW.match(line)
        if not m:
            continue
        path = m.group(5)
        acc = ACCESSION.search(path)
        if not acc or acc.group(1) in seen:
            continue
        seen.add(acc.group(1))
        out.append(IndexEntry(acc.group(1), datetime.strptime(m.group(4), "%Y%m%d").date(), path))
    return out


def _text(node, path):
    el = node.find(path)
    return (el.text or "").strip() if el is not None else ""


def _flag(node, path):
    return _text(node, path).lower() in TRUE


def _num(s):
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_form4(accession, filed, raw):
    """Parse one filing's text into purchase rows.

    Returns None if no parseable ownership document was found (so the caller can record it),
    or a (possibly empty) list of purchase dicts.
    """
    m = OWNERSHIP_DOC.search(raw)
    if not m:
        return None
    try:
        root = ET.fromstring(m.group(0))
    except ET.ParseError:
        return None
    if _text(root, "documentType") not in ("4", ""):
        return []

    symbol = _text(root, "issuer/issuerTradingSymbol").upper()
    issuer_cik = _text(root, "issuer/issuerCik")
    issuer_name = _text(root, "issuer/issuerName")
    plan_flag = any((el.text or "").strip().lower() in TRUE for el in root.iter("aff10b5One"))
    footnotes = {fn.get("id"): " ".join("".join(fn.itertext()).split()) for fn in root.iter("footnote")}

    owners = []
    for ro in root.findall("reportingOwner"):
        owners.append({
            "cik": _text(ro, "reportingOwnerId/rptOwnerCik"),
            "name": _text(ro, "reportingOwnerId/rptOwnerName"),
            "officer": _flag(ro, "reportingOwnerRelationship/isOfficer"),
            "director": _flag(ro, "reportingOwnerRelationship/isDirector"),
            "ten": _flag(ro, "reportingOwnerRelationship/isTenPercentOwner"),
            "title": _text(ro, "reportingOwnerRelationship/officerTitle"),
        })
    if not owners or not symbol:
        return []
    owner = next((o for o in owners if o["officer"] or o["director"]), owners[0])

    rows = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if _text(tx, "transactionCoding/transactionCode").upper() != "P":
            continue
        if _text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value").upper() != "A":
            continue
        if _flag(tx, "transactionCoding/equitySwapInvolved"):
            continue
        shares = _num(_text(tx, "transactionAmounts/transactionShares/value"))
        price = _num(_text(tx, "transactionAmounts/transactionPricePerShare/value"))
        tx_date = _text(tx, "transactionDate/value")[:10]
        try:
            date.fromisoformat(tx_date)
        except ValueError:
            continue
        if not shares or not price or shares <= 0 or price <= 0:
            continue
        note_ids = {el.get("id") for el in tx.iter("footnoteId")}
        plan_note = any(PLAN_NOTE.search(footnotes.get(i, "")) for i in note_ids)
        rows.append({
            "accession": accession,
            "issuer_cik": issuer_cik,
            "symbol": symbol,
            "issuer_name": issuer_name,
            "owner_cik": owner["cik"],
            "owner_name": owner["name"],
            "title": owner["title"],
            "is_officer": int(owner["officer"]),
            "is_director": int(owner["director"]),
            "is_ten_pct": int(owner["ten"]),
            "tx_date": tx_date,
            "filed": filed.isoformat(),
            "shares": shares,
            "price": price,
            "value": round(shares * price, 2),
            "tenb5": int(plan_flag or plan_note),
        })
    return rows


class EdgarClient:
    def __init__(self, user_agent, delay=0.15, session=None, sleep=time.sleep):
        if "@" not in (user_agent or ""):
            raise SystemExit(
                'SEC_USER_AGENT must identify you with a contact email, e.g. "Jane Doe jane@example.com". '
                "The SEC blocks anonymous automated access."
            )
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.delay = delay
        self._sleep = sleep
        self._last = 0.0

    def _get(self, url, missing_ok=False, retries=3):
        for attempt in range(retries):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.session.get(url, timeout=30)
            except requests.RequestException as exc:
                log.warning("Request error for %s: %s", url, exc)
                self._sleep(2 ** attempt * 2)
                continue
            code = resp.status_code
            if code == 200:
                return resp.text
            if code == 404 or (code == 403 and missing_ok):
                return None   # EDGAR answers 403 or 404 for files that do not exist (weekends, holidays)
            if code in (403, 429) or code >= 500:
                log.warning("HTTP %s for %s (attempt %d)", code, url, attempt + 1)
                self._sleep(2 ** attempt * 2)
                continue
            raise EdgarError(f"HTTP {code} for {url}")
        raise EdgarError(f"giving up on {url}")

    def get_index(self, day):
        return self._get(daily_index_url(day), missing_ok=True)

    def get_filing(self, path):
        text = self._get(ARCHIVES + path)
        if text is None:
            raise EdgarError(f"filing not found: {path}")
        return text


def collect(client, state, today, days, max_failures=10):
    """Scan the last `days` calendar days of indexes; store purchases from unseen Form 4s."""
    stats = {"indexes": 0, "new_filings": 0, "purchases": 0, "failed": 0, "unparseable": 0}
    for offset in range(days, -1, -1):
        day = today - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        text = client.get_index(day)
        if text is None:
            log.info("No daily index for %s (holiday, not published yet, or blocked)", day)
            continue
        stats["indexes"] += 1
        for entry in parse_index(text):
            if state.filing_known(entry.accession):
                continue
            try:
                raw = client.get_filing(entry.path)
            except EdgarError as exc:
                stats["failed"] += 1
                log.error("Could not fetch %s: %s", entry.accession, exc)
                if stats["failed"] > max_failures:
                    raise EdgarError("too many failed downloads; stopping (check SEC_USER_AGENT / rate limit)")
                continue
            rows = parse_form4(entry.accession, entry.filed, raw)
            if rows is None:
                stats["unparseable"] += 1
                state.mark_filing(entry.accession, entry.filed, "unparseable")
                continue
            state.add_buys(rows)
            state.mark_filing(entry.accession, entry.filed, "ok")
            stats["new_filings"] += 1
            stats["purchases"] += len(rows)
    if stats["indexes"] == 0:
        log.warning("Found no daily indexes at all. If this is not a holiday week, check SEC_USER_AGENT "
                    "and your network.")
    log.info("EDGAR collection: %s", stats)
    return stats

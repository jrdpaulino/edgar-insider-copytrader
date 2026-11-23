"""Alpaca downloads for the multi-signal research: tradable universe, daily bars, news article counts.

Nothing here places orders. Calls are throttled for the free plan (200 requests/minute) and are resumable:
re-running a download skips what is already stored.
"""
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

DATA_BASE = "https://data.alpaca.markets"
ET = ZoneInfo("America/New_York")
EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"}
SYMBOL_OK = re.compile(r"^[A-Z]{1,5}$")
NAME_BAD = re.compile(r"warrant|\brights?\b|\bunits?\b|preferred|depositary|\bnotes?\b|\betf\b|\bfund\b|\btrust\b",
                      re.I)


class DataError(RuntimeError):
    pass


class AccessDenied(DataError):
    """HTTP 401/403: bad keys, or the data is not included in the account's plan."""


def effective_day(created_at):
    """ISO date on which a news item becomes usable: its New York date, or the next day if published after
    the 4 pm close (16:00 + 8 hours rolls over midnight). Returns None if the timestamp can't be parsed."""
    try:
        dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.fromisoformat(str(created_at)[:19])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(ET) + timedelta(hours=8)).date().isoformat()


class DataClient:
    def __init__(self, key_id, secret, paper=True, session=None, min_interval=0.35, sleep=time.sleep):
        if not (key_id and secret):
            raise SystemExit("ALPACA_KEY_ID / ALPACA_SECRET_KEY are not set (put them in .env).")
        self.trading_base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        self.session = session or requests.Session()
        self.session.headers.update({"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret})
        self.min_interval, self._sleep, self._last = min_interval, sleep, 0.0

    def _get(self, url, params=None, retries=5):
        for attempt in range(retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.session.get(url, params=params, timeout=60)
            except requests.RequestException:
                self._sleep(2 ** attempt)
                continue
            code = resp.status_code
            if code == 200:
                return resp.json()
            if code in (401, 403):
                raise AccessDenied(f"HTTP {code} from {url}: {resp.text[:200]}")
            if code == 429 or code >= 500:
                self._sleep(2 ** attempt * 2)
                continue
            raise DataError(f"HTTP {code} from {url}: {resp.text[:200]}")
        raise DataError(f"giving up on {url}")

    def tradable_symbols(self):
        """Active, tradable US common-stock-like tickers (name heuristics drop ETFs, funds, warrants, etc.).

        Note this lists CURRENTLY listed names only, so any backtest on it has survivorship bias."""
        items = self._get(self.trading_base + "/v2/assets", {"status": "active", "asset_class": "us_equity"})
        out = set()
        for a in items:
            sym = a.get("symbol") or ""
            if (a.get("tradable") and a.get("exchange") in EXCHANGES and SYMBOL_OK.match(sym)
                    and not NAME_BAD.search(a.get("name") or "")):
                out.add(sym)
        return sorted(out)

    def bars(self, symbols, start, end, feed="iex"):
        """Daily adjusted bars for several symbols in one paginated call: {symbol: [(day, o, c, v), ...]}."""
        out, token = defaultdict(list), None
        while True:
            params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start.isoformat(),
                      "end": end.isoformat(), "adjustment": "all", "feed": feed, "limit": 10000}
            if token:
                params["page_token"] = token
            body = self._get(DATA_BASE + "/v2/stocks/bars", params)
            bars = body.get("bars") or {}
            if not isinstance(bars, dict):
                raise DataError("unexpected bars response shape (expected a symbol-to-bars mapping)")
            for sym, items in bars.items():
                out[sym] += [(b["t"][:10], b["o"], b["c"], b["v"]) for b in items]
            token = body.get("next_page_token")
            if not token:
                return out

    def news_day(self, day, max_symbols=5):
        """Count one UTC day's articles per (effective_day, symbol).

        Articles tagged with more than `max_symbols` tickers (market round-ups) are skipped as noise.
        Returns (Counter, total_articles_seen)."""
        start = f"{day.isoformat()}T00:00:00Z"
        end = f"{(day + timedelta(days=1)).isoformat()}T00:00:00Z"
        counts, articles, token = Counter(), 0, None
        while True:
            params = {"start": start, "end": end, "sort": "asc", "limit": 50, "include_content": "false"}
            if token:
                params["page_token"] = token
            body = self._get(DATA_BASE + "/v1beta1/news", params)
            items = body.get("news")
            if items is None:
                raise DataError("unexpected news response shape (no 'news' field)")
            for item in items:
                articles += 1
                tagged = item.get("symbols") or []
                eff = effective_day(item.get("created_at"))
                if eff is None or not tagged or len(tagged) > max_symbols:
                    continue
                for sym in {s for s in tagged if SYMBOL_OK.match(s)}:
                    counts[(eff, sym)] += 1
            token = body.get("next_page_token")
            if not token:
                return counts, articles


def download_bars(client, store, symbols, start, end, chunk=50, min_bars=250, min_dollar_volume=50_000.0,
                  always_keep=("SPY", "IWM"), progress=print):
    """Download bars for symbols not yet screened. Stores only liquid, long-enough histories."""
    done = store.statuses()
    todo = [s for s in dict.fromkeys(list(always_keep) + list(symbols)) if s not in done]
    if not todo:
        progress("   nothing new to download")
        return
    for k in range(0, len(todo), chunk):
        batch = todo[k:k + chunk]
        data = client.bars(batch, start, end)
        for sym in batch:
            rows = data.get(sym, [])
            if sym in always_keep and rows:
                store.save_symbol(sym, "kept", rows)
            elif len(rows) < min_bars:
                store.save_symbol(sym, "short")
            elif statistics.median(c * v for _, _, c, v in rows) < min_dollar_volume:
                store.save_symbol(sym, "illiquid")
            else:
                store.save_symbol(sym, "kept", rows)
        store.commit()
        progress(f"   bars: {min(k + chunk, len(todo))}/{len(todo)} symbols processed")


def download_news(client, store, start, end, max_symbols=5, progress=print):
    """Download news counts for each calendar day in [start, end] that is not stored yet."""
    done = store.news_days_done()
    day, fetched = start, 0
    while day <= end:
        iso = day.isoformat()
        if iso not in done:
            counts, articles = client.news_day(day, max_symbols)
            store.save_news_day(iso, counts, articles)
            fetched += 1
            if fetched % 20 == 0:
                progress(f"   news: downloaded through {iso} ({fetched} days this run)")
        day += timedelta(days=1)
    progress(f"   news: done ({fetched} new days)")

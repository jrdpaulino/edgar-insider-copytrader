"""SQLite storage for the multi-signal research data: daily bars, symbol screening results, news counts."""
import sqlite3
from collections import defaultdict


class PanelStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS bars (
                symbol TEXT, day TEXT, o REAL, c REAL, v REAL, PRIMARY KEY (symbol, day));
            CREATE TABLE IF NOT EXISTS symbols (
                symbol TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE IF NOT EXISTS news_days (
                day TEXT PRIMARY KEY, articles INTEGER);
            CREATE TABLE IF NOT EXISTS news_counts (
                day TEXT, symbol TEXT, n REAL, PRIMARY KEY (day, symbol));
            """
        )

    # ----- bars and screening
    def statuses(self):
        return dict(self.db.execute("SELECT symbol, status FROM symbols"))

    def save_symbol(self, symbol, status, rows=None):
        """Record a screening verdict ('kept', 'short', 'illiquid') and, for kept symbols, the daily bars."""
        if rows:
            self.db.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?)",
                                [(symbol, d, o, c, v) for d, o, c, v in rows])
        self.db.execute("INSERT OR REPLACE INTO symbols VALUES (?,?)", (symbol, status))

    def commit(self):
        self.db.commit()

    def kept_symbols(self):
        return sorted(s for s, st in self.statuses().items() if st == "kept")

    def load_bars(self, symbol):
        return self.db.execute("SELECT day, o, c, v FROM bars WHERE symbol=? ORDER BY day", (symbol,)).fetchall()

    # ----- news
    def news_days_done(self):
        return {r[0] for r in self.db.execute("SELECT day FROM news_days")}

    def save_news_day(self, day, counts, articles):
        """Counts and the 'day is done' marker land in one transaction, so a crash never double-counts."""
        with self.db:
            self.db.executemany(
                "INSERT INTO news_counts VALUES (?,?,?) ON CONFLICT(day, symbol) DO UPDATE SET n = n + excluded.n",
                [(d, s, n) for (d, s), n in counts.items()])
            self.db.execute("INSERT OR REPLACE INTO news_days VALUES (?,?)", (day, articles))

    def load_news(self):
        """Return {symbol: {effective_day: article_count}}."""
        out = defaultdict(dict)
        for day, symbol, n in self.db.execute("SELECT day, symbol, n FROM news_counts"):
            out[symbol][day] = n
        return out

    def news_range(self):
        """(first_day, last_day) of downloaded news days, or None."""
        lo, hi = self.db.execute("SELECT MIN(day), MAX(day) FROM news_days").fetchone()
        return (lo, hi) if lo else None

"""SQLite state: parsed filings, insider purchases, handled signals, orders, and open lots."""
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone


@dataclass(frozen=True)
class Lot:
    symbol: str
    entry_day: date
    notional: float
    order_id: str


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS filings (
                accession TEXT PRIMARY KEY, filed TEXT, status TEXT);
            CREATE TABLE IF NOT EXISTS insider_buys (
                accession TEXT, idx INTEGER, issuer_cik TEXT, symbol TEXT, issuer_name TEXT,
                owner_cik TEXT, owner_name TEXT, title TEXT, is_officer INTEGER, is_director INTEGER,
                is_ten_pct INTEGER, tx_date TEXT, filed TEXT, shares REAL, price REAL, value REAL,
                tenb5 INTEGER, PRIMARY KEY (accession, idx));
            CREATE INDEX IF NOT EXISTS ix_buys_filed ON insider_buys(filed);
            CREATE TABLE IF NOT EXISTS seen (
                id TEXT PRIMARY KEY, ts TEXT, status TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, day TEXT, symbol TEXT,
                side TEXT, notional REAL, broker_order_id TEXT, status TEXT,
                dry_run INTEGER, reason TEXT);
            CREATE TABLE IF NOT EXISTS lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, entry_day TEXT, notional REAL,
                order_id TEXT, status TEXT, closed_day TEXT, note TEXT);
            """
        )
        self.db.row_factory = sqlite3.Row

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # filings and purchases
    def filing_known(self, accession):
        return self.db.execute("SELECT 1 FROM filings WHERE accession=?", (accession,)).fetchone() is not None

    def mark_filing(self, accession, filed, status):
        self.db.execute("INSERT OR REPLACE INTO filings VALUES (?,?,?)", (accession, str(filed), status))
        self.db.commit()

    def add_buys(self, rows):
        for i, r in enumerate(rows):
            self.db.execute(
                "INSERT OR REPLACE INTO insider_buys VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["accession"], i, r["issuer_cik"], r["symbol"], r["issuer_name"], r["owner_cik"],
                 r["owner_name"], r["title"], r["is_officer"], r["is_director"], r["is_ten_pct"],
                 r["tx_date"], r["filed"], r["shares"], r["price"], r["value"], r["tenb5"]),
            )
        self.db.commit()

    def buys_since(self, since):
        cur = self.db.execute("SELECT * FROM insider_buys WHERE filed >= ?", (since.isoformat(),))
        return [dict(r) for r in cur.fetchall()]

    # handled signals
    def seen_ids(self):
        return {r[0] for r in self.db.execute("SELECT id FROM seen")}

    def mark(self, ids, status, note=""):
        self.db.executemany(
            "INSERT OR REPLACE INTO seen VALUES (?,?,?,?)", [(i, self._now(), status, note) for i in ids]
        )
        self.db.commit()

    # orders
    def log_order(self, day, symbol, side, notional, broker_order_id, status, dry_run, reason=""):
        self.db.execute(
            "INSERT INTO orders (ts, day, symbol, side, notional, broker_order_id, status, dry_run, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self._now(), str(day), symbol, side, notional, broker_order_id, status, int(dry_run), reason),
        )
        self.db.commit()

    def spent_today(self, day, dry_run):
        row = self.db.execute(
            "SELECT COALESCE(SUM(notional), 0) FROM orders WHERE day=? AND side='buy' AND dry_run=?",
            (str(day), int(dry_run)),
        ).fetchone()
        return float(row[0])

    # lots: positions this agent opened (only these are ever closed by the agent)
    def open_lot(self, symbol, day, notional, order_id):
        self.db.execute(
            "INSERT INTO lots (symbol, entry_day, notional, order_id, status) VALUES (?,?,?,?, 'open')",
            (symbol, str(day), notional, order_id),
        )
        self.db.commit()

    def open_lots(self):
        cur = self.db.execute("SELECT symbol, entry_day, notional, order_id FROM lots WHERE status='open'")
        return [Lot(r["symbol"], date.fromisoformat(r["entry_day"]), r["notional"], r["order_id"])
                for r in cur.fetchall()]

    def open_lot_symbols(self):
        return {lot.symbol for lot in self.open_lots()}

    def close_lot(self, symbol, day, note=""):
        self.db.execute(
            "UPDATE lots SET status='closed', closed_day=?, note=? WHERE symbol=? AND status='open'",
            (str(day), note, symbol),
        )
        self.db.commit()

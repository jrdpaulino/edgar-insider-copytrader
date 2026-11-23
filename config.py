"""Configuration, loaded from environment variables (and an optional .env file).

Safety defaults: paper trading ON, dry-run ON. Real orders against a live
account require PAPER=false, DRY_RUN=false AND CONFIRM_LIVE_TRADING=I_UNDERSTAND_THE_RISKS.
"""
import os
from dataclasses import dataclass


def load_dotenv(path=".env"):
    """Tiny .env reader so we need no extra dependency."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as fh:   # utf-8-sig also tolerates the BOM some Windows editors add
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name, default):
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "y", "on")


def _list(name, upper=False):
    items = [x.strip() for x in os.getenv(name, "").split(",")]
    return tuple((x.upper() if upper else x) for x in items if x)


@dataclass(frozen=True)
class Config:
    # credentials / identity
    sec_user_agent: str = ""            # SEC requires a contact, e.g. "Jane Doe jane@example.com"
    alpaca_key_id: str = ""
    alpaca_secret: str = ""

    # safety switches
    paper: bool = True
    dry_run: bool = True

    # sizing / risk limits
    max_order_usd: float = 250.0        # flat size of each new position
    max_daily_usd: float = 1000.0       # total new buys per day
    max_position_pct: float = 0.05      # max share of equity in one ticker
    max_open_positions: int = 20
    max_new_per_run: int = 5            # cap on new positions opened in one run

    # EDGAR collection
    filing_lookback_days: int = 14      # calendar days of daily indexes scanned each run
    request_delay: float = 0.15         # seconds between SEC requests (SEC limit: 10/s)

    # signal definition (insider cluster buying)
    window_days: int = 14               # cluster window, by filing date
    max_tx_age_days: int = 10           # ignore purchases older than this
    min_insiders: int = 2               # distinct officers/directors buying
    min_total_usd: float = 50000.0      # combined purchase value for a cluster
    single_buy_usd: float = 250000.0    # a lone CEO/CFO purchase at/above this also qualifies
    min_price: float = 3.0              # skip sub-$3 stocks
    max_premium_pct: float = 0.10       # don't chase: skip while price is >10% above insiders' average
    hold_days: int = 90                 # close positions this many calendar days after entry
    ticker_blocklist: tuple = ()

    # files
    db_path: str = "edgar_state.db"
    kill_switch_path: str = "STOP"      # create this file to halt all trading

    @classmethod
    def from_env(cls):
        load_dotenv()
        cfg = cls(
            sec_user_agent=os.getenv("SEC_USER_AGENT", ""),
            alpaca_key_id=os.getenv("ALPACA_KEY_ID", ""),
            alpaca_secret=os.getenv("ALPACA_SECRET_KEY", ""),
            paper=_bool("PAPER", True),
            dry_run=_bool("DRY_RUN", True),
            max_order_usd=float(os.getenv("MAX_ORDER_USD", 250)),
            max_daily_usd=float(os.getenv("MAX_DAILY_USD", 1000)),
            max_position_pct=float(os.getenv("MAX_POSITION_PCT", 0.05)),
            max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", 20)),
            max_new_per_run=int(os.getenv("MAX_NEW_PER_RUN", 5)),
            filing_lookback_days=int(os.getenv("FILING_LOOKBACK_DAYS", 14)),
            request_delay=float(os.getenv("REQUEST_DELAY", 0.15)),
            window_days=int(os.getenv("WINDOW_DAYS", 14)),
            max_tx_age_days=int(os.getenv("MAX_TX_AGE_DAYS", 10)),
            min_insiders=int(os.getenv("MIN_INSIDERS", 2)),
            min_total_usd=float(os.getenv("MIN_TOTAL_USD", 50000)),
            single_buy_usd=float(os.getenv("SINGLE_BUY_USD", 250000)),
            min_price=float(os.getenv("MIN_PRICE", 3.0)),
            max_premium_pct=float(os.getenv("MAX_PREMIUM_PCT", 0.10)),
            hold_days=int(os.getenv("HOLD_DAYS", 90)),
            ticker_blocklist=_list("TICKER_BLOCKLIST", upper=True),
            db_path=os.getenv("DB_PATH", "edgar_state.db"),
            kill_switch_path=os.getenv("KILL_SWITCH_PATH", "STOP"),
        )
        cfg.validate()
        return cfg

    def validate(self):
        if not self.paper and not self.dry_run:
            if os.getenv("CONFIRM_LIVE_TRADING") != "I_UNDERSTAND_THE_RISKS":
                raise SystemExit(
                    "Refusing to place REAL orders. Set CONFIRM_LIVE_TRADING=I_UNDERSTAND_THE_RISKS "
                    "in your environment if you really mean it, or use PAPER=true."
                )
        if self.max_order_usd <= 0 or self.max_daily_usd < self.max_order_usd:
            raise SystemExit("MAX_ORDER_USD must be > 0 and <= MAX_DAILY_USD.")
        if not 0 < self.max_position_pct <= 1:
            raise SystemExit("MAX_POSITION_PCT must be between 0 and 1.")
        if self.min_insiders < 1 or self.hold_days < 1 or self.window_days < 1:
            raise SystemExit("MIN_INSIDERS, HOLD_DAYS and WINDOW_DAYS must be >= 1.")
        if self.filing_lookback_days < self.window_days:
            raise SystemExit("FILING_LOOKBACK_DAYS must be >= WINDOW_DAYS (clusters need that history).")

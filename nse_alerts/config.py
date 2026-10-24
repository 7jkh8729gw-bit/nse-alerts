"""All tunable settings in one place. Every value can be overridden with an
environment variable of the same name (e.g. export RVOL_MIN_A=1.8)."""
import os
from datetime import datetime, timedelta, timezone, time as dtime

# India has no daylight saving, so a fixed +05:30 offset is exact (no tzdata needed).
IST = timezone(timedelta(hours=5, minutes=30))


def _f(name, default):
    return float(os.getenv(name, default))


def _i(name, default):
    return int(os.getenv(name, default))


def now_ist() -> datetime:
    return datetime.now(IST)


def today_str() -> str:
    return now_ist().strftime("%Y-%m-%d")


BROKER = os.getenv("BROKER", "upstox").lower()               # "upstox" (default) or "angel"
DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
DAILY_STORE = os.path.join(DATA_DIR, "daily.csv.gz")
CONTEXT_FILE = os.path.join(DATA_DIR, "context.json")        # today's watch contexts
STATE_FILE = os.path.join(DATA_DIR, "alert_state.json")      # dedupe + open positions
ALERT_LOG = os.path.join(DATA_DIR, "alerts_log.csv")         # every alert + outcome
SCRIP_MASTER_FILE = os.path.join(DATA_DIR, f"scrip_master_{BROKER}.json")

# ---------------- Secrets (never hard-code) ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE", "")
ANGEL_PIN = os.getenv("ANGEL_PIN", "")                 # Angel One MPIN
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "")

UPSTOX_ANALYTICS_TOKEN = os.getenv("UPSTOX_ANALYTICS_TOKEN", "")   # free, read-only, valid 1 year
UPSTOX_INSTRUMENTS_URL = os.getenv(
    "UPSTOX_INSTRUMENTS_URL", "https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz")

# ---------------- Universe (pre-market) ----------------
# Liquidity is measured in RUPEES traded, not share count: 500k shares of a
# Rs 9,000 stock is hugely liquid, 500k shares of a Rs 12 stock is not.
MIN_MEDIAN_TURNOVER_CR = _f("MIN_MEDIAN_TURNOVER_CR", 5.0)   # 20-day median traded value, Rs crore
MIN_PRICE = _f("MIN_PRICE", 50.0)
MAX_DIST_TO_52W_HIGH_PCT = _f("MAX_DIST_TO_52W_HIGH_PCT", 15.0)
MAX_DIST_TO_PIVOT_PCT = _f("MAX_DIST_TO_PIVOT_PCT", 8.0)     # must be able to trigger today
PIVOT_LOOKBACK = _i("PIVOT_LOOKBACK", 20)                    # breakout = above 20-session high
A_LIST_SIZE = _i("A_LIST_SIZE", 40)                          # top-ranked names, looser live thresholds
MAX_SUBSCRIPTIONS = _i("MAX_SUBSCRIPTIONS", 800)             # live-monitored universe (B-list incl.)
JUMP_GUARD_PCT = _f("JUMP_GUARD_PCT", 35.0)                  # drop names with unexplained 1-day jumps

# ---------------- Live alert rules ----------------
ORB_MINUTES = _i("ORB_MINUTES", 15)                          # opening range = 09:15-09:30
ALERT_START = dtime(9, 30)
ALERT_END = dtime(*map(int, os.getenv("ALERT_END", "14:00").split(":")))
LATE_WINDOW_START = dtime(13, 0)                             # 13:00-14:00 needs extra volume
LATE_RVOL_MULT = _f("LATE_RVOL_MULT", 1.25)
RVOL_MIN_A = _f("RVOL_MIN_A", 1.5)
RVOL_MIN_B = _f("RVOL_MIN_B", 2.5)
ZONE_MIN_PCT = _f("ZONE_MIN_PCT", 0.6)                       # entry zone above pivot: clamp(0.35*ATR%, 0.6, 1.5)
ZONE_MAX_PCT = _f("ZONE_MAX_PCT", 1.5)
MAX_DAY_CHANGE_PCT = _f("MAX_DAY_CHANGE_PCT", 8.0)           # near circuit = can't buy reliably
SL_BELOW_PIVOT_ATR = _f("SL_BELOW_PIVOT_ATR", 0.4)
MAX_RISK_ATR = _f("MAX_RISK_ATR", 1.0)
MIN_RISK_PCT = _f("MIN_RISK_PCT", 0.8)
T1_R = _f("T1_R", 1.5)
T2_R = _f("T2_R", 3.0)
MAX_ALERTS_PER_DAY = _i("MAX_ALERTS_PER_DAY", 10)
MAX_B_ALERTS_PER_DAY = _i("MAX_B_ALERTS_PER_DAY", 4)
MARKET_BLOCK_PCT = _f("MARKET_BLOCK_PCT", -0.8)              # Nifty proxy below this: no new alerts
MARKET_CAUTION_PCT = _f("MARKET_CAUTION_PCT", -0.3)          # below this: need extra volume
MARKET_PROXY_SYMBOL = os.getenv("MARKET_PROXY_SYMBOL", "NIFTYBEES")

# ---------------- Plumbing ----------------
WATCHDOG_STALE_SECONDS = _i("WATCHDOG_STALE_SECONDS", 90)
MAX_BAR_AGE_SECONDS = _i("MAX_BAR_AGE_SECONDS", 180)         # never alert from a bar older than this
NSE_BHAV_URL = os.getenv(
    "NSE_BHAV_URL",
    "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date}.csv")
SCRIP_MASTER_URL = os.getenv(
    "SCRIP_MASTER_URL",
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json")
HISTORY_DAYS = _i("HISTORY_DAYS", 400)                       # calendar days kept (~270 sessions; need >=200)

"""Upstox adapter (all trading + data APIs are free; uses the read-only Analytics Token,
valid 1 year, no daily login, no static IP needed for market data).

Same interface as angel.py: load_token_map(), UpstoxClient.login()/.candles(), Stream.
Ticks are converted to a broker-neutral dict that the engine accepts directly.
"""
import base64
import gzip
import io
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests

from . import config

log = logging.getLogger("upstox")
API = "https://api.upstox.com"


class UpstoxError(RuntimeError):
    pass


def load_token_map(force: bool = False) -> dict:
    """{'RELIANCE': 'NSE_EQ|INE002A01018', ...} from Upstox's public instrument file."""
    path = config.SCRIP_MASTER_FILE
    if not force and os.path.exists(path) and \
            datetime.fromtimestamp(os.path.getmtime(path)).date() == datetime.now().date():
        return json.load(open(path))
    r = requests.get(config.UPSTOX_INSTRUMENTS_URL, timeout=120)
    r.raise_for_status()
    raw = r.content
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass                                           # already plain CSV
    df = pd.read_csv(io.BytesIO(raw))
    df = df[df["instrument_key"].astype(str).str.startswith("NSE_EQ|")]
    if "instrument_type" in df.columns:
        df = df[df["instrument_type"].astype(str).str.upper() == "EQUITY"]
    m = dict(zip(df["tradingsymbol"].astype(str).str.upper(), df["instrument_key"].astype(str)))
    if len(m) < 500:
        raise UpstoxError(f"Instrument file looks wrong ({len(m)} NSE equities)")
    json.dump(m, open(path, "w"))
    return m


def token_days_left(token: str) -> Optional[int]:
    """Days until the (JWT) token expires, or None if it can't be read."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return int((datetime.fromtimestamp(exp, tz=timezone.utc) - datetime.now(timezone.utc)).days)
    except Exception:
        return None


def _interval(name: str):
    return {"ONE_MINUTE": ("minutes", "1"), "FIVE_MINUTE": ("minutes", "5"),
            "FIFTEEN_MINUTE": ("minutes", "15"), "ONE_DAY": ("days", "1")}[name]


class UpstoxClient:
    def __init__(self):
        self.token = config.UPSTOX_ANALYTICS_TOKEN
        self.last_call = 0.0

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def login(self):
        if not self.token:
            raise UpstoxError("UPSTOX_ANALYTICS_TOKEN is not set")
        r = requests.get(f"{API}/v2/market-quote/ltp", headers=self._headers(),
                         params={"instrument_key": "NSE_INDEX|Nifty 50"}, timeout=20)
        if r.status_code == 401:
            raise UpstoxError("Upstox token rejected (expired or revoked): generate a new Analytics Token")
        if r.status_code != 200:
            raise UpstoxError(f"Upstox check failed: HTTP {r.status_code} {r.text[:200]}")
        left = token_days_left(self.token)
        log.info("Upstox token OK%s", f" ({left} days left)" if left is not None else "")

    def candles(self, token: str, interval: str, start: str, end: str) -> list:
        """Rows [timestamp, o, h, l, c, volume] oldest-first, between 'YYYY-MM-DD HH:MM' bounds."""
        unit, n = _interval(interval)
        key = requests.utils.quote(token, safe="")
        d0, d1 = start[:10], end[:10]
        today = config.now_ist().strftime("%Y-%m-%d")
        url = (f"{API}/v3/historical-candle/intraday/{key}/{unit}/{n}" if d0 == d1 == today
               else f"{API}/v3/historical-candle/{key}/{unit}/{n}/{d1}/{d0}")
        for attempt in range(3):
            wait = 0.25 - (time.time() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.time()
            try:
                r = requests.get(url, headers=self._headers(), timeout=30)
                if r.status_code == 200:
                    rows = (r.json().get("data") or {}).get("candles") or []
                    rows = sorted(rows, key=lambda x: x[0])        # API returns newest first
                    lo, hi = start.replace(" ", "T"), end.replace(" ", "T")
                    return [x for x in rows if lo <= x[0][:16] <= hi]
                log.warning("candles %s HTTP %s %s", token, r.status_code, r.text[:150])
            except Exception as e:
                log.warning("candles %s attempt %d: %s", token, attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
        return []


def normalize(message: dict, now: Optional[datetime] = None) -> list:
    """Upstox MarketDataStreamerV3 message (already protobuf->dict) -> neutral tick dicts."""
    out = []
    for key, f in (message.get("feeds") or {}).items():
        ff = (f.get("fullFeed") or {}).get("marketFF") or f.get("ltpc") and {"ltpc": f["ltpc"]} or {}
        ltpc = ff.get("ltpc") or {}
        try:
            ltp = float(ltpc.get("ltp") or 0)
        except (TypeError, ValueError):
            continue
        if ltp <= 0:
            continue
        ltt = ltpc.get("ltt")
        ts = datetime.fromtimestamp(int(ltt) / 1000.0, tz=config.IST) if ltt else (now or config.now_ist())
        day_open = None
        for o in (ff.get("marketOHLC") or {}).get("ohlc") or []:
            if o.get("interval") == "1d" and o.get("open"):
                day_open = float(o["open"])
        vtt = ff.get("vtt")
        out.append({"_norm": True, "token": key, "price": ltp, "ts": ts,
                    "cum_vol": float(vtt) if vtt not in (None, "") else None,
                    "vwap": float(ff.get("atp") or 0.0), "day_open": day_open})
    return out


class Stream:
    """Live feed via the official SDK, 'full' mode (price + volume + day VWAP)."""

    def __init__(self, client: UpstoxClient, tokens: list, on_tick):
        self.client, self.tokens, self.on_tick = client, list(dict.fromkeys(tokens)), on_tick
        self.streamer = None
        self.thread = None
        self._opened = self._closed = False
        self._started = 0.0

    def start(self):
        import upstox_client                           # pip install upstox-python-sdk
        cfg = upstox_client.Configuration()
        cfg.access_token = self.client.token
        batches = [self.tokens[i:i + 100] for i in range(0, len(self.tokens), 100)]
        self.streamer = upstox_client.MarketDataStreamerV3(upstox_client.ApiClient(cfg), batches[0], "full")

        def on_open(*_):
            self._opened = True
            log.info("Upstox feed open; subscribing %d instruments", len(self.tokens))
            for b in batches[1:]:
                self.streamer.subscribe(b, "full")
                time.sleep(0.3)

        def on_message(message, *_):
            try:
                for t in normalize(message):
                    self.on_tick(t)
            except Exception:
                log.exception("tick error")

        def on_close(*_):
            self._closed = True
            log.warning("Upstox feed closed")

        def on_error(*a):
            log.warning("Upstox feed error: %s", a[-1] if a else "")

        self.streamer.on("open", on_open)
        self.streamer.on("message", on_message)
        self.streamer.on("close", on_close)
        self.streamer.on("error", on_error)
        self._started = time.time()
        self.thread = threading.Thread(target=self.streamer.connect, daemon=True)
        self.thread.start()

    @property
    def alive(self) -> bool:
        if self._closed:
            return False
        return self._opened or time.time() - self._started < 30   # 30 s grace to open

    def stop(self):
        try:
            if self.streamer:
                self.streamer.disconnect()
        except Exception as e:
            log.debug("disconnect: %s", e)
        self._closed = True

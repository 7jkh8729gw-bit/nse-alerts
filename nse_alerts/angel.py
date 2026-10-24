"""Angel One SmartAPI wrapper (free API, free live websocket, free historical data).

Everything that touches the broker lives here; the SDK is imported lazily so the
rest of the system (and the unit tests) work without it installed.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime

import requests

from . import config

log = logging.getLogger("angel")


class AngelError(RuntimeError):
    pass


def load_token_map(force: bool = False) -> dict:
    """{'RELIANCE': '2885', ...} for NSE cash-market EQ symbols (public file, no login)."""
    path = config.SCRIP_MASTER_FILE
    if not force and os.path.exists(path) and \
            datetime.fromtimestamp(os.path.getmtime(path)).date() == datetime.now().date():
        return json.load(open(path))
    r = requests.get(config.SCRIP_MASTER_URL, timeout=120)
    r.raise_for_status()
    m = {}
    for row in r.json():
        sym = str(row.get("symbol", ""))
        if row.get("exch_seg") == "NSE" and sym.endswith("-EQ"):
            m[sym[:-3]] = str(row["token"])
    if len(m) < 500:
        raise AngelError(f"Scrip master looks wrong ({len(m)} NSE EQ symbols)")
    json.dump(m, open(path, "w"))
    return m


class AngelClient:
    def __init__(self):
        self.api = None
        self.jwt = None
        self.feed_token = None
        self._last_call = 0.0

    def login(self):
        missing = [k for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET")
                   if not getattr(config, k)]
        if missing:
            raise AngelError("Missing environment variables: " + ", ".join(missing))
        from SmartApi import SmartConnect      # pip install smartapi-python
        import pyotp
        self.api = SmartConnect(api_key=config.ANGEL_API_KEY)
        totp = pyotp.TOTP(config.ANGEL_TOTP_SECRET).now()
        data = self.api.generateSession(config.ANGEL_CLIENT_CODE, config.ANGEL_PIN, totp)
        if not data or not data.get("status"):
            raise AngelError(f"Angel login failed: {data}")
        self.jwt = data["data"]["jwtToken"]
        self.feed_token = self.api.getfeedToken()
        log.info("Angel login OK")

    def candles(self, token: str, interval: str, start: str, end: str) -> list:
        """interval e.g. ONE_MINUTE / FIVE_MINUTE / ONE_DAY; times 'YYYY-MM-DD HH:MM'.
        Returns rows [timestamp, open, high, low, close, volume]. Rate-limited (~3/s)."""
        for attempt in range(3):
            wait = 0.4 - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
            try:
                res = self.api.getCandleData({"exchange": "NSE", "symboltoken": str(token),
                                              "interval": interval, "fromdate": start, "todate": end})
                if res and res.get("status") and res.get("data") is not None:
                    return res["data"]
                log.warning("candles %s: %s", token, res)
            except Exception as e:
                log.warning("candles %s attempt %d: %s", token, attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
        return []


class Stream:
    """Websocket session subscribed in QUOTE mode (LTP + cumulative volume + day VWAP)."""
    CORR_ID = "nsealerts1"      # 10 alphanumeric characters, as the API requires
    EXCHANGE_NSE_CM = 1
    MODE_QUOTE = 2

    def __init__(self, client: AngelClient, tokens: list, on_tick):
        self.client, self.tokens, self.on_tick = client, list(dict.fromkeys(tokens)), on_tick
        self.sws = None
        self.thread = None
        self.error = None

    def start(self):
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        c = self.client
        self.sws = SmartWebSocketV2(c.jwt, config.ANGEL_API_KEY, config.ANGEL_CLIENT_CODE, c.feed_token)

        def on_open(*_):
            log.info("Websocket open; subscribing %d tokens", len(self.tokens))
            for i in range(0, len(self.tokens), 100):
                batch = [{"exchangeType": self.EXCHANGE_NSE_CM, "tokens": self.tokens[i:i + 100]}]
                self.sws.subscribe(self.CORR_ID, self.MODE_QUOTE, batch)
                time.sleep(0.3)

        def on_data(_ws, message):
            try:
                self.on_tick(message)
            except Exception:
                log.exception("on_tick error")

        def on_error(*args):
            self.error = args[-1] if args else "unknown"
            log.warning("Websocket error: %s", self.error)

        def on_close(*_):
            log.warning("Websocket closed")

        self.sws.on_open, self.sws.on_data = on_open, on_data
        self.sws.on_error, self.sws.on_close = on_error, on_close
        self.thread = threading.Thread(target=self.sws.connect, daemon=True)
        self.thread.start()

    @property
    def alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def stop(self):
        try:
            if self.sws:
                self.sws.close_connection()
        except Exception as e:
            log.debug("close: %s", e)

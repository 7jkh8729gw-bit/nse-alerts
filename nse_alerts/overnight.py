"""Overnight / global markets snapshot for the pre-market Telegram message.

Uses Upstox's free 'Global Instruments' file (indices + indicators) and the Full Market
Quote API with your read-only Analytics Token. Best-effort: ANY failure just means no
message is sent; it can never break the watchlist.

    python -m nse_alerts.overnight           # send to Telegram
    python -m nse_alerts.overnight --print   # print only + show a raw quote (for checking)
"""
import argparse
import gzip
import json
import logging
import re
import sys

import requests

from . import config, notify, upstox

log = logging.getLogger("overnight")
GLOBAL_URL = "https://assets.upstox.com/market-quote/instruments/exchange/global.json.gz"
# (label shown in Telegram, regex matched against the instrument's name / trading symbol)
WANTED = [
    ("GIFT Nifty", r"^GIFT NIFTY$|SGX NIFTY"),
    ("S&P 500", r"S&P ?500|\^GSPC|^US 500$|^SPX$"),
    ("Dow", r"^DOW JONES|\^DJI|^US 30$"),
    ("Nasdaq 100", r"US TECH 100|\^NDX|NASDAQ ?100"),
    ("Nikkei", r"NIKKEI"),
    ("Brent", r"BRENT|BZUSD"),
    ("USD/INR", r"USD ?INR|USDINR"),
]


def load_instruments() -> list:
    r = requests.get(GLOBAL_URL, timeout=30)
    r.raise_for_status()
    raw = r.content
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    return json.loads(raw)


def pick(instruments: list) -> list:
    chosen = []
    for label, pattern in WANTED:
        rx = re.compile(pattern, re.I)
        for ins in instruments:
            if rx.search(str(ins.get("name", ""))) or rx.search(str(ins.get("trading_symbol", ""))):
                chosen.append((label, ins["instrument_key"]))
                break
    return chosen


def fetch_quotes(keys: list, token: str) -> dict:
    r = requests.get(f"{upstox.API}/v2/market-quote/quotes",
                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                     params={"instrument_key": ",".join(keys)}, timeout=20)
    r.raise_for_status()
    return r.json().get("data") or {}


def find_quote(data: dict, key: str):
    for cand in (key, key.replace("|", ":")):
        if cand in data:
            return data[cand]
    for v in data.values():
        if isinstance(v, dict) and v.get("instrument_token") == key:
            return v
    return None


def last_and_pct(q: dict):
    try:
        last = float(q.get("last_price"))
    except (TypeError, ValueError):
        return None, None
    net = q.get("net_change")
    try:
        if net is not None and last - float(net):
            return last, float(net) / (last - float(net)) * 100
        prev = (q.get("ohlc") or {}).get("close")
        if prev:
            return last, (last / float(prev) - 1) * 100
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return last, None


def build_text(chosen: list, data: dict) -> str:
    parts = []
    for label, key in chosen:
        q = find_quote(data, key)
        if not q:
            continue
        last, pct = last_and_pct(q)
        if last is None:
            continue
        txt = f"{label} {last:,.0f}" if last >= 1000 else f"{label} {last:,.2f}"
        if pct is not None:
            txt += f" ({pct:+.1f}%)"
        parts.append(txt)
    if not parts:
        return ""
    return ("🌍 <b>Overnight markets</b> (latest readings)\n" + " · ".join(parts) +
            "\n<i>US markets are closed at this hour. Index data can lag 2–15 minutes.</i>")


def summary(show_raw: bool = False) -> str:
    token = config.UPSTOX_ANALYTICS_TOKEN
    if not token:
        return ""
    try:
        chosen = pick(load_instruments())
        if not chosen:
            log.warning("no global instruments matched")
            return ""
        data = fetch_quotes([k for _, k in chosen], token)
        if show_raw:
            first = next(iter(data.items()), None)
            print("Matched:", chosen)
            print("First raw quote:", json.dumps(first, default=str)[:600])
        return build_text(chosen, data)
    except Exception as e:      # never break the pre-market run
        log.warning("overnight snapshot failed: %s", e)
        return ""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()
    text = summary(show_raw=args.print)
    if args.print:
        print(text or "(nothing to show - see log lines above)")
    elif text:
        notify.send(text)
    sys.exit(0)

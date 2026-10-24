"""Whole-market daily history from NSE's free end-of-day 'bhavcopy' file.

One small download per trading day covers ALL ~2,000 stocks (OHLC, volume,
traded value, delivery %). That replaces ~2,000 sequential Yahoo calls per scan.

Corporate actions: NSE's PREV_CLOSE is the *adjusted* base price on ex-dates, so
`PREV_CLOSE(today) / stored_close(yesterday)` != 1 reveals splits/bonuses and we
back-adjust history (guarded against a missed download day, which would otherwise
look like a mass-adjustment).
"""
import io
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import requests

from . import config

log = logging.getLogger("daily_store")
COLS = ["date", "symbol", "open", "high", "low", "close", "prev_close",
        "volume", "turnover_cr", "deliv_pct"]
PRICE_COLS = ["open", "high", "low", "close", "prev_close"]
ADJ_BAND = 0.03          # |factor-1| above this = corporate action
MASS_ADJ_FRACTION = 0.03  # >3% of symbols "adjusting" at once = we missed a day, skip
MIN_SYMBOLS_FOR_GUARD = 200  # the percentage test is only meaningful on a realistic universe


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                      "Accept": "text/csv,*/*", "Referer": "https://www.nseindia.com/"})
    return s


def parse_bhavcopy(text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text), skipinitialspace=True)
    df.columns = [str(c).strip().upper() for c in df.columns]
    df["SERIES"] = df["SERIES"].astype(str).str.strip()
    df = df[df["SERIES"] == "EQ"].copy()   # BE/T-group stocks cannot be traded intraday
    out = pd.DataFrame({
        "date": pd.to_datetime(df["DATE1"].astype(str).str.strip(), format="%d-%b-%Y"),
        "symbol": df["SYMBOL"].astype(str).str.strip().str.upper(),
        "open": pd.to_numeric(df["OPEN_PRICE"], errors="coerce"),
        "high": pd.to_numeric(df["HIGH_PRICE"], errors="coerce"),
        "low": pd.to_numeric(df["LOW_PRICE"], errors="coerce"),
        "close": pd.to_numeric(df["CLOSE_PRICE"], errors="coerce"),
        "prev_close": pd.to_numeric(df["PREV_CLOSE"], errors="coerce"),
        "volume": pd.to_numeric(df["TTL_TRD_QNTY"], errors="coerce"),
        "turnover_cr": pd.to_numeric(df["TURNOVER_LACS"], errors="coerce") / 100.0,
        "deliv_pct": pd.to_numeric(df["DELIV_PER"], errors="coerce"),  # '-' -> NaN
    })
    return out.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)


def fetch_day(d: date, sess: Optional[requests.Session] = None):
    """Returns (status, DataFrame|None). status: 'ok' | 'missing' (holiday/not yet
    published) | 'error' (network/blocked)."""
    sess = sess or _session()
    url = config.NSE_BHAV_URL.format(date=d.strftime("%d%m%Y"))
    for attempt in range(3):
        try:
            r = sess.get(url, timeout=30)
            if r.status_code == 404:
                return "missing", None
            if r.status_code == 200 and "SYMBOL" in r.text[:500].upper():
                return "ok", parse_bhavcopy(r.text)
            log.warning("bhavcopy %s -> HTTP %s", d, r.status_code)
        except Exception as e:
            log.warning("bhavcopy %s attempt %s failed: %s", d, attempt + 1, e)
        time.sleep(2 * (attempt + 1))
    return "error", None


def adjust_for_actions(store: pd.DataFrame, day: pd.DataFrame) -> pd.DataFrame:
    """Back-adjust `store` for splits/bonuses revealed by `day.prev_close`."""
    if store.empty:
        return store
    last_close = store.sort_values("date").groupby("symbol")["close"].last()
    m = day.set_index("symbol")["prev_close"]
    common = m.index.intersection(last_close.index)
    factor = (m.loc[common] / last_close.loc[common]).replace([float("inf")], float("nan")).dropna()
    adj = factor[(factor - 1).abs() > ADJ_BAND]
    if len(common) >= MIN_SYMBOLS_FOR_GUARD and len(adj) / len(common) > MASS_ADJ_FRACTION:
        log.warning("%d/%d symbols look 'adjusted' - probably a missed day; skipping adjustment",
                    len(adj), len(common))
        return store
    if adj.empty:
        return store
    store = store.copy()
    # Work in plain numpy so the result does not depend on pandas version / dtype quirks.
    adj_map = {str(k): float(v) for k, v in adj.items()}
    f = pd.to_numeric(store["symbol"].astype(str).map(adj_map), errors="coerce").to_numpy(dtype="float64")
    mask = ~np.isnan(f)
    for c in PRICE_COLS:
        arr = pd.to_numeric(store[c], errors="coerce").to_numpy(dtype="float64", copy=True)
        arr[mask] = arr[mask] * f[mask]
        store[c] = arr
    vol = pd.to_numeric(store["volume"], errors="coerce").to_numpy(dtype="float64", copy=True)
    vol[mask] = vol[mask] / f[mask]
    store["volume"] = vol
    log.info("Adjusted history for %d corporate action(s): %s", len(adj),
             ", ".join(f"{s}x{v:.3f}" for s, v in adj.head(10).items()))
    return store


def append_days(store: pd.DataFrame, days: list) -> pd.DataFrame:
    for day in sorted(days, key=lambda d: d["date"].iloc[0]):
        if not store.empty and day["date"].iloc[0] in set(store["date"].unique()):
            continue
        store = adjust_for_actions(store, day)
        store = pd.concat([store, day[COLS]], ignore_index=True)
    return store


def load() -> pd.DataFrame:
    if not os.path.exists(config.DAILY_STORE):
        return pd.DataFrame(columns=COLS)
    df = pd.read_csv(config.DAILY_STORE, parse_dates=["date"])
    for c in PRICE_COLS + ["volume", "turnover_cr", "deliv_pct"]:
        df[c] = df[c].astype("float64")
    return df[COLS]


def save(store: pd.DataFrame) -> None:
    cutoff = pd.Timestamp(datetime.now().date() - timedelta(days=config.HISTORY_DAYS))
    store = store[store["date"] >= cutoff].sort_values(["symbol", "date"])
    tmp = config.DAILY_STORE + ".tmp"
    store.to_csv(tmp, index=False, compression="gzip", float_format="%.4f")
    os.replace(tmp, config.DAILY_STORE)


def update(seed_days: Optional[int] = None) -> pd.DataFrame:
    """Bring the store up to date (auto-seeds if empty). Returns the store."""
    store = load()
    sess = _session()
    today = config.now_ist().date()
    if store.empty or seed_days:
        start = today - timedelta(days=seed_days or config.HISTORY_DAYS)
        log.info("Seeding history from %s (one request per day, ~0.4s each)...", start)
    else:
        start = store["date"].max().date() + timedelta(days=1)
    days, errors, d = [], 0, start
    while d <= today:
        if d.weekday() < 5 and (store.empty or pd.Timestamp(d) not in set(store["date"].unique())):
            status, df = fetch_day(d, sess)
            if status == "ok":
                days.append(df)
            elif status == "error":
                errors += 1
            time.sleep(0.4)
        d += timedelta(days=1)
    if errors:
        log.warning("%d day(s) could not be downloaded (network/blocked).", errors)
    store = append_days(store, days)
    if days:
        save(store)
        log.info("Store now %d rows, latest session %s", len(store), store["date"].max().date())
    return store

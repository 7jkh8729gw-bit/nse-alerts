"""Measure the REAL intraday volume curve from ~20 sessions of 5-minute candles of
liquid stocks and save it to data/profile.json (used automatically by signals.py).

    python -m nse_alerts.calibrate [--symbols 40] [--days 28]
"""
import argparse
import json
import logging
import os
from collections import defaultdict
from datetime import timedelta

import numpy as np

from . import broker, config, daily_store

MARKS = [15, 30, 60, 120, 180, 240, 300, 345]
OPEN_MIN = 9 * 60 + 15


def profile_from_candles(rows_by_symbol: dict) -> list:
    """rows: [ts 'YYYY-MM-DDTHH:MM:...', o, h, l, c, v]. Returns knots [(minute, cum_fraction)]."""
    fr = defaultdict(list)
    for rows in rows_by_symbol.values():
        days = defaultdict(list)
        for r in rows:
            days[r[0][:10]].append((int(r[0][11:13]) * 60 + int(r[0][14:16]) - OPEN_MIN, float(r[5])))
        for cand in days.values():
            if len(cand) < 70:                       # skip half-days / bad days (full day = 75 bars)
                continue
            total = sum(v for _, v in cand)
            if total <= 0:
                continue
            for m in MARKS:
                fr[m].append(sum(v for off, v in cand if off < m) / total)
    if not fr:
        raise RuntimeError("No usable candle data")
    med = np.maximum.accumulate([float(np.median(fr[m])) for m in MARKS])
    return [(0, 0.0)] + [(m, float(f)) for m, f in zip(MARKS, med.round(4))] + [(375, 1.0)]


def main():
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=40)
    ap.add_argument("--days", type=int, default=28)
    a = ap.parse_args()
    store = daily_store.load()
    top = (store.groupby("symbol")["turnover_cr"].median().sort_values(ascending=False)
           .head(a.symbols).index.tolist())
    tmap, client = broker.load_token_map(), broker.make_client()
    client.login()
    end = config.now_ist().date()
    start = end - timedelta(days=a.days)
    data = {}
    for s in top:
        if s in tmap:
            data[s] = client.candles(tmap[s], "FIVE_MINUTE", f"{start} 09:15", f"{end} 15:30")
    knots = profile_from_candles(data)
    json.dump([list(k) for k in knots], open(os.path.join(config.DATA_DIR, "profile.json"), "w"))
    print("Saved calibrated volume curve:")
    for m, f in knots:
        print(f"  {m:3d} min after open: {f * 100:5.1f}% of the day's volume")


if __name__ == "__main__":
    main()

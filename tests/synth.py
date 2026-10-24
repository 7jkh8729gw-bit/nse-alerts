"""Synthetic market data for offline tests."""
import numpy as np
import pandas as pd


def make_symbol(sym, n=260, seed=0, kind="coil", base=100.0, end_date="2026-10-16", turnover_cr=20.0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=end_date, periods=n)
    drift = {"coil": 0.0012, "trend": 0.0010, "down": -0.0012, "flat": 0.0}[kind]
    ret = rng.normal(drift, 0.015, n)
    if kind == "coil":                       # last 15 sessions: tight, quiet, near the high
        ret[-15:] = rng.normal(0.0003, 0.0035, 15)
    c = base * np.exp(np.cumsum(ret))
    spread = np.abs(rng.normal(0.01, 0.003, n)) * c
    if kind == "coil":
        spread[-15:] = 0.004 * c[-15:]
    h, l = c + spread * 0.6, c - spread * 0.4
    o = np.r_[c[0], c[:-1]]
    v = rng.normal(1_000_000, 100_000, n).clip(2e5)
    if kind == "coil":
        v[-6:-1] *= 0.6
        v[-1] *= 1.6; c[-1] = h[-1] = max(h[-1], c[-1]) * 0.9999; l[-1] = c[-1] * 0.985
    return pd.DataFrame({"date": dates, "symbol": sym, "open": o, "high": np.maximum(h, np.maximum(o, c)),
                         "low": np.minimum(l, np.minimum(o, c)), "close": c,
                         "prev_close": o, "volume": v,
                         "turnover_cr": v * c / 1e7 * (turnover_cr / (1e6 * base / 1e7)),
                         "deliv_pct": rng.normal(45, 5, n)})


def make_market(n_syms=60, seed=1):
    frames = []
    kinds = ["coil", "trend", "down", "flat"]
    for i in range(n_syms):
        frames.append(make_symbol(f"S{i:03d}", seed=seed + i, kind=kinds[i % 4], base=80 + i))
    return pd.concat(frames, ignore_index=True)

"""Does the pre-market ranking actually find next-day breakouts?  (daily-bar test)

For each past session t: screen with data up to t, take the top names, then look at
session t+1. A name "triggers" if it trades through its pivot without gapping
beyond the entry zone. Outcome is scored in R using the SAME stop/target rules as
live trading. Daily bars cannot show intra-day ordering, so the test is
CONSERVATIVE: if a bar touches both stop and target it counts as a loss.

    python -m nse_alerts.premarket --seed-days 700     # ~480 sessions of history
    python -m nse_alerts.backtest --sessions 150

It compares the A-list against 'every gated name' and against 'all liquid stocks'
so you can see whether the ranking adds value. It cannot test intraday volume rules.
"""
import argparse

import numpy as np
import pandas as pd

from . import config, daily_store, scoring, signals


def simulate_next_day(feat: pd.Series, nxt: pd.Series):
    """Return (triggered, R) for one name on the following session."""
    pivot, atr = feat["pivot"], feat["atr"]
    if nxt["high"] < pivot:
        return False, 0.0
    entry = max(pivot, nxt["open"])
    zone_hi = pivot * (1 + signals.entry_zone_pct(feat["atr_pct"]) / 100)
    if entry > zone_hi:
        return False, 0.0                      # gapped past the entry zone: no alert would fire
    risk = entry - (pivot - config.SL_BELOW_PIVOT_ATR * atr)
    risk = min(max(risk, entry * config.MIN_RISK_PCT / 100), config.MAX_RISK_ATR * atr)
    sl, t1 = entry - risk, entry + config.T1_R * risk
    if nxt["low"] <= sl:
        return True, -1.0                      # conservative on same-bar touches
    if nxt["high"] >= t1:
        return True, config.T1_R
    return True, (nxt["close"] - entry) / risk


def run(store: pd.DataFrame, sessions: int = 150, top_n: int = None):
    top_n = top_n or config.A_LIST_SIZE
    groups = {s: g.sort_values("date").reset_index(drop=True) for s, g in store.groupby("symbol")}
    dates = sorted(store["date"].unique())
    test_dates = dates[-(sessions + 1):-1]
    res = {"A-list (top %d)" % top_n: [], "Top 10": [], "All gated names": [], "All liquid, no filter": []}
    for d in test_dates:
        rows = []
        for sym, g in groups.items():
            idx = g.index[g["date"] == d]
            if len(idx) == 0 or idx[0] + 1 >= len(g):
                continue
            f = scoring.symbol_features(g, int(idx[0]))
            if f:
                f["symbol"], f["_next"] = sym, g.iloc[idx[0] + 1]
                rows.append(f)
        df = scoring.add_rs_rank(pd.DataFrame(rows))
        if df.empty:
            continue
        gated = scoring.score(scoring.apply_gates(df, pd.Timestamp(d)))
        liquid = df[df["median_turnover_cr"] >= config.MIN_MEDIAN_TURNOVER_CR]
        for key, sel in (("A-list (top %d)" % top_n, gated.head(top_n)), ("Top 10", gated.head(10)),
                         ("All gated names", gated), ("All liquid, no filter", liquid)):
            for _, r in sel.iterrows():
                res[key].append((r["symbol"], d) + simulate_next_day(r, r["_next"]))
    print(f"\nBacktest over {len(test_dates)} sessions ({pd.Timestamp(test_dates[0]).date()} "
          f"to {pd.Timestamp(test_dates[-1]).date()})\n")
    print(f"{'Group':26s}{'picks':>8s}{'triggered':>11s}{'win%':>7s}{'avg R':>8s}{'R per pick':>12s}")
    for k, v in res.items():
        trig = [x for x in v if x[2]]
        if not v:
            continue
        rs = np.array([x[3] for x in trig]) if trig else np.array([0.0])
        win = (rs > 0).mean() * 100
        print(f"{k:26s}{len(v):8d}{len(trig):11d}{win:7.1f}{rs.mean():8.2f}{sum(x[3] for x in trig) / len(v):12.3f}")
    print("\nHow to read this: the ranking only adds value if 'A-list' clearly beats 'All liquid' on avg R "
          "over many hundreds of triggers.\nDaily bars are a rough sanity check, not proof of live profitability.")
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=150)
    run(daily_store.load(), ap.parse_args().sessions)

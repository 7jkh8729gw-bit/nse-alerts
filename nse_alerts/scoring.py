"""Pre-market screening: gates (must-pass) + a 0-100 setup score (ranking).

Gates keep only tradable stocks in a confirmed uptrend that are close enough to
their breakout level to trigger TODAY. The score then ranks them by the
ingredients of high-quality breakouts: proximity, relative strength, volatility
contraction, accumulation volume, delivery, trend quality. Weights are sensible
PRIORS (not fitted to 43 trades); validate with `python -m nse_alerts.backtest`.
"""
import numpy as np
import pandas as pd

from . import config

MIN_SESSIONS = 200
W = dict(proximity=20, rs=20, coil=20, volume=15, delivery=10, trend=15)


def _clip(x, lo=0.0, hi=1.0):
    return float(min(max(x, lo), hi))


def symbol_features(g: pd.DataFrame, asof_idx: int = None) -> dict | None:
    """Features from one symbol's history up to row `asof_idx` (default: last row)."""
    if asof_idx is not None:
        g = g.iloc[: asof_idx + 1]
    if len(g) < MIN_SESSIONS:
        return None
    o, h, l, c = (g[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    v = g["volume"].to_numpy(float)
    to = g["turnover_cr"].to_numpy(float)
    dp = g["deliv_pct"].to_numpy(float)
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    close = c[-1]
    n52 = min(250, len(c))
    lb = config.PIVOT_LOOKBACK
    sma50, sma200 = c[-50:].mean(), c[-200:].mean()
    sma50_prev = c[-60:-10].mean()
    atr14, atr10, atr50 = tr[-14:].mean(), tr[-10:].mean(), tr[-50:].mean()
    pivot = h[-lb:].max()
    high52 = h[-n52:].max()
    rng = h[-1] - l[-1]
    clv = (c[-1] - l[-1]) / rng if rng > 0 else 0.5
    avg_vol20 = v[-20:].mean()
    rets = c[1:] / c[:-1] - 1
    dp_prev = dp[-21:-1]
    dp_prev = dp_prev[~np.isnan(dp_prev)]
    return dict(
        date=g["date"].iloc[-1], close=close, pivot=pivot, high52=high52,
        sma50=sma50, sma200=sma200, sma50_slope_pct=(sma50 / sma50_prev - 1) * 100,
        atr=atr14, atr_pct=atr14 / close * 100,
        atr_ratio=atr10 / atr50 if atr50 > 0 else 1.0,
        range10_pct=(h[-10:].max() - l[-10:].min()) / close * 100,
        dist_pivot_pct=(pivot / close - 1) * 100,
        dist_52w_pct=(high52 / close - 1) * 100,
        median_turnover_cr=float(np.median(to[-20:])),
        avg_vol20=avg_vol20,
        vol5_ratio=v[-5:].mean() / v[-50:].mean() if v[-50:].mean() > 0 else 1.0,
        yday_vol_ratio=v[-1] / avg_vol20 if avg_vol20 > 0 else 1.0,
        yday_chg_pct=(c[-1] / c[-2] - 1) * 100, clv=clv,
        deliv_ratio=(dp[-1] / dp_prev.mean()) if len(dp_prev) >= 10 and not np.isnan(dp[-1])
        and dp_prev.mean() > 0 else np.nan,
        ret63=c[-1] / c[-64] - 1, ret126=c[-1] / c[-127] - 1 if len(c) > 127 else np.nan,
        max_jump_pct=float(np.abs(rets[-250:]).max() * 100),
        prev_close=close,
    )


def add_rs_rank(df: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional relative strength among LIQUID names (a percentile is far
    more robust than an absolute-return threshold that changes with the market)."""
    if df.empty:
        return df
    liq = df["median_turnover_cr"] >= config.MIN_MEDIAN_TURNOVER_CR
    blend = df["ret63"].rank(pct=True) * 0.6 + df["ret126"].fillna(df["ret63"]).rank(pct=True) * 0.4
    ref = np.sort(blend[liq].to_numpy())
    df["rs_rank"] = (np.searchsorted(ref, blend.to_numpy(), side="left") / len(ref) * 100) if len(ref) else 0.0
    return df


def build_feature_table(store: pd.DataFrame, asof: pd.Timestamp = None) -> pd.DataFrame:
    rows = []
    for sym, g in store.groupby("symbol", sort=False):
        g = g.sort_values("date")
        if asof is not None:
            g = g[g["date"] <= asof]
        f = symbol_features(g)
        if f:
            f["symbol"] = sym
            rows.append(f)
    return add_rs_rank(pd.DataFrame(rows))


def apply_gates(df: pd.DataFrame, asof: pd.Timestamp = None) -> pd.DataFrame:
    if df.empty:
        return df
    m = (
        (df["close"] >= config.MIN_PRICE)
        & (df["median_turnover_cr"] >= config.MIN_MEDIAN_TURNOVER_CR)
        & (df["close"] > df["sma50"]) & (df["sma50"] > df["sma200"])
        & (df["sma50_slope_pct"] > 0)
        & (df["dist_52w_pct"] <= config.MAX_DIST_TO_52W_HIGH_PCT)
        & (df["dist_pivot_pct"] <= config.MAX_DIST_TO_PIVOT_PCT)
        & (df["max_jump_pct"] < config.JUMP_GUARD_PCT)     # unexplained jump = bad data
        & (df["yday_chg_pct"] < 12)                        # don't chase yesterday's circuit
    )
    if asof is not None:                                   # drop suspended / stale names
        m &= (asof - df["date"]).dt.days <= 5
    return df[m].copy()


def score(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    prox = df["dist_pivot_pct"].apply(lambda d: 1.0 if d <= 1 else _clip(1 - (d - 1) / 7))
    rs = df["rs_rank"].apply(lambda r: _clip((r - 50) / 50))
    coil = (df["atr_ratio"].apply(lambda r: _clip((1 - r) / 0.4)) * 0.5
            + df["range10_pct"].apply(lambda r: _clip((10 - r) / 7)) * 0.5)
    acc = (df["yday_vol_ratio"].apply(lambda r: _clip(r - 1)) *
           ((df["clv"] >= 0.65) & (df["yday_chg_pct"] > 0)))
    dry = df["vol5_ratio"].apply(lambda r: 0.7 if r <= 0.8 else (0.35 if r <= 1.0 else 0.0))
    vol = np.maximum(acc, dry)
    deliv = df["deliv_ratio"].apply(lambda r: 0.0 if pd.isna(r) else _clip((r - 1) / 0.5)) \
        * (df["yday_chg_pct"] > 0)
    trend = (df["dist_52w_pct"].apply(lambda d: _clip(1 - d / config.MAX_DIST_TO_52W_HIGH_PCT)) * 0.6
             + df["sma50_slope_pct"].apply(lambda s: _clip(s / 3)) * 0.4)
    df["score"] = (prox * W["proximity"] + rs * W["rs"] + coil * W["coil"] + vol * W["volume"]
                   + deliv * W["delivery"] + trend * W["trend"]).round(1)
    def tag(i):
        if acc.loc[i] >= 0.3:
            return "Accumulation day"
        if coil.loc[i] >= 0.5:
            return "Volatility coil"
        return "Trend near pivot"
    df["setup"] = [tag(i) for i in df.index]
    return df.sort_values("score", ascending=False).reset_index(drop=True)


def screen(store: pd.DataFrame, asof: pd.Timestamp = None):
    """Full pipeline. Returns (universe_liquid_count, scored_gated_df)."""
    feats = build_feature_table(store, asof)
    if feats.empty:
        return 0, feats
    asof = asof or store["date"].max()
    liquid = int((feats["median_turnover_cr"] >= config.MIN_MEDIAN_TURNOVER_CR).sum())
    return liquid, score(apply_gates(feats, asof))

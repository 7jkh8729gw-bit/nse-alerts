"""Pure live-signal logic: no network, no clock, no I/O -> fully unit-testable.

Why each rule exists (see README "Why these criteria" for the evidence):
  * RVOL is cumulative volume vs. the volume EXPECTED BY THIS TIME OF DAY (NSE
    volume is U-shaped), not "last bar vs. the previous 12 bars" which makes any
    normal afternoon bar look like a spike.
  * Alerts fire on a CLOSED 1-minute bar above the level (not a single tick) and
    only while price is still inside a tight entry zone above the level, so you
    are not told to buy something that already ran 6-8%.
  * Stops sit just under the breakout level (structure), not 1.2x DAILY ATR away.
"""
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Optional

from . import config

# Cumulative share of a typical NSE session's volume traded by N minutes after
# the 09:15 open. A reasonable U-shaped DEFAULT - calibrate it with
# `python -m nse_alerts.calibrate` once you have broker access.
PROFILE_KNOTS = [(0, 0.0), (15, 0.11), (30, 0.18), (60, 0.29), (120, 0.45),
                 (180, 0.58), (240, 0.68), (300, 0.79), (345, 0.90), (375, 1.0)]


def _load_profile():
    """Use a calibrated curve (data/profile.json from `python -m nse_alerts.calibrate`) if present."""
    import json, os
    path = os.path.join(config.DATA_DIR, "profile.json")
    try:
        if os.path.exists(path):
            knots = [tuple(map(float, k)) for k in json.load(open(path))]
            if knots[0][0] == 0 and knots[-1][1] > 0.99:
                return knots
    except Exception:
        pass
    return PROFILE_KNOTS


PROFILE_KNOTS = _load_profile()


def minutes_since_open(t: datetime) -> float:
    return (t.hour * 60 + t.minute + t.second / 60.0) - (9 * 60 + 15)


def expected_cum_fraction(minutes: float, knots=None) -> float:
    knots = knots or PROFILE_KNOTS
    if minutes <= knots[0][0]:
        return knots[0][1]
    for (m0, f0), (m1, f1) in zip(knots, knots[1:]):
        if minutes <= m1:
            return f0 + (f1 - f0) * (minutes - m0) / (m1 - m0)
    return knots[-1][1]


def rvol(cum_vol: float, avg_vol20: float, t: datetime, knots=None) -> Optional[float]:
    """Relative volume vs. time-of-day expectation. None if not computable."""
    if not avg_vol20 or avg_vol20 <= 0 or cum_vol is None:
        return None
    frac = expected_cum_fraction(minutes_since_open(t), knots)
    if frac < 0.05:          # first ~6 minutes: too noisy to judge
        return None
    return cum_vol / (avg_vol20 * frac)


def entry_zone_pct(atr_pct: float) -> float:
    return min(config.ZONE_MAX_PCT, max(config.ZONE_MIN_PCT, 0.35 * atr_pct))


@dataclass
class Ctx:
    symbol: str
    token: str
    tier: str                 # "A" (top-ranked) or "B" (rest of gated universe)
    pivot: float              # highest high of the last N completed sessions
    atr: float                # daily ATR(14), rupees
    atr_pct: float
    avg_vol20: float
    prev_close: float
    score: float = 0.0
    setup: str = ""
    rs_rank: float = 0.0
    dist_pivot_pct: float = 0.0


@dataclass
class Snap:
    price: float
    cum_vol: float
    vwap: float = 0.0
    orb_high: Optional[float] = None
    orb_low: Optional[float] = None
    day_open: Optional[float] = None


@dataclass
class Bar:
    t: datetime               # minute start
    o: float
    h: float
    l: float
    c: float
    vol: float = 0.0


@dataclass
class Decision:
    fire: bool
    reason: str
    entry: float = 0.0
    sl: float = 0.0
    t1: float = 0.0
    t2: float = 0.0
    risk_pct: float = 0.0
    level: float = 0.0
    zone_hi: float = 0.0
    rvol: float = 0.0
    day_change_pct: float = 0.0
    tags: list = field(default_factory=list)


def rvol_threshold(ctx: Ctx, t: datetime, market_change_pct: float) -> float:
    thr = config.RVOL_MIN_A if ctx.tier == "A" else config.RVOL_MIN_B
    if t.time() >= config.LATE_WINDOW_START:
        thr *= config.LATE_RVOL_MULT
    if market_change_pct is not None and market_change_pct <= config.MARKET_CAUTION_PCT:
        thr *= 1.2
    return thr


def evaluate(ctx: Ctx, bar: Bar, snap: Snap, market_change_pct: Optional[float],
             alerts_today: int, b_alerts_today: int, knots=None) -> Decision:
    """Decide whether the just-CLOSED 1-minute `bar` is a buy alert."""
    t = bar.t
    tm = t.time()
    if tm < config.ALERT_START or tm > config.ALERT_END:
        return Decision(False, "outside alert window")
    if market_change_pct is not None and market_change_pct <= config.MARKET_BLOCK_PCT:
        return Decision(False, f"market weak ({market_change_pct:+.2f}%)")
    if alerts_today >= config.MAX_ALERTS_PER_DAY:
        return Decision(False, "daily alert cap")
    if ctx.tier == "B" and b_alerts_today >= config.MAX_B_ALERTS_PER_DAY:
        return Decision(False, "daily B-list cap")

    price = bar.c
    if ctx.prev_close <= 0 or price <= 0:
        return Decision(False, "bad prices")
    day_chg = (price / ctx.prev_close - 1) * 100
    if day_chg <= 0:
        return Decision(False, "not above previous close", day_change_pct=day_chg)
    if day_chg > config.MAX_DAY_CHANGE_PCT:
        return Decision(False, "too extended / near circuit", day_change_pct=day_chg)

    level = ctx.pivot
    tags = [f"{config.PIVOT_LOOKBACK}d high"]
    if snap.orb_high and snap.orb_high > level:
        level = snap.orb_high
        tags = ["ORB high (above 20d high)"]
    elif snap.orb_high:
        tags.append("ORB break")
    if price <= level:
        return Decision(False, "below breakout level", level=level)
    if bar.c < bar.o:
        return Decision(False, "breakout bar is red", level=level)
    zone_hi = level * (1 + entry_zone_pct(ctx.atr_pct) / 100)
    if price > zone_hi:
        return Decision(False, "already extended beyond entry zone", level=level, zone_hi=zone_hi)
    if snap.vwap and price <= snap.vwap:
        return Decision(False, "below VWAP", level=level)
    r = rvol(snap.cum_vol, ctx.avg_vol20, t, knots)
    if r is None:
        return Decision(False, "rvol unavailable", level=level)
    need = rvol_threshold(ctx, t, market_change_pct)
    if r < need:
        return Decision(False, f"rvol {r:.2f} < {need:.2f}", level=level, rvol=r)

    atr = ctx.atr if ctx.atr > 0 else price * 0.02
    risk = price - (level - config.SL_BELOW_PIVOT_ATR * atr)
    risk = min(max(risk, price * config.MIN_RISK_PCT / 100), config.MAX_RISK_ATR * atr)
    sl = price - risk
    tags += ["above VWAP", f"RVOL {r:.1f}x"]
    return Decision(True, "BUY", entry=price, sl=sl, t1=price + config.T1_R * risk,
                    t2=price + config.T2_R * risk, risk_pct=risk / price * 100,
                    level=level, zone_hi=zone_hi, rvol=r, day_change_pct=day_chg, tags=tags)


def heads_up(ctx: Ctx, snap: Snap, now: datetime, market_change_pct: Optional[float]) -> bool:
    """'About to break out' early warning (A-list only): within 0.3% under the level
    with volume already running. Sent silently, once per symbol per day."""
    if ctx.tier != "A" or now.time() < config.ALERT_START or now.time() > config.ALERT_END:
        return False
    if market_change_pct is not None and market_change_pct <= config.MARKET_BLOCK_PCT:
        return False
    level = max(ctx.pivot, snap.orb_high or 0)
    if not (level * 0.997 <= snap.price < level):
        return False
    if snap.vwap and snap.price <= snap.vwap:
        return False
    r = rvol(snap.cum_vol, ctx.avg_vol20, now)
    return r is not None and r >= config.RVOL_MIN_A * 0.8

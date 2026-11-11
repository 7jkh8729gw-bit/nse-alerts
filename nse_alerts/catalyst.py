"""News-driven buy alerts.

Flow: exchange announcements after the last close (and during the day) -> catalyst watchlist (silent,
background only) -> live price/volume trigger -> ONE buy alert per stock with entry, stop and targets.

News says WHERE to look. The market's reaction says WHEN: a catalyst stock is only alerted when
  * it is up 1-7% on the day (moving, but not already at the peak),
  * it breaks the high of its recent range / opening range (a closed 1-minute bar, still inside a tight zone),
  * it trades above VWAP,
  * its volume pace over the last ~5 minutes is >= 3x normal for that time of day (and total >= 1.5x),
  * between 09:30 and 14:30, market not weak, and under the daily cap.

    python -m nse_alerts.catalyst [--max-minutes 345]      # live news engine (action: catalyst)
"""
import argparse
import logging
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, time as dtime, timedelta

from . import broker, config, daily_store, news, notify, radar, run_day, scoring, signals
from .engine import Engine
from .signals import Bar, Decision

log = logging.getLogger("catalyst")
NEWS_END = dtime(14, 30)
MIN_CHG = float(os.getenv("NEWS_MIN_CHG_PCT", "1.0"))
MAX_CHG = float(os.getenv("NEWS_MAX_CHG_PCT", "4.0"))
RVOL_MIN = float(os.getenv("NEWS_RVOL", "1.5"))
ACCEL_MIN = float(os.getenv("NEWS_ACCEL", "3.0"))
REFRESH_SECONDS = int(os.getenv("NEWS_REFRESH_SECONDS", "300"))
STATE_FILE = os.path.join(config.DATA_DIR, "catalyst_state.json")
LOG_FILE = os.path.join(config.DATA_DIR, "catalyst_log.csv")


def evaluate_news(ctx, bar: Bar, prev_bars: list, snap, accel, market_chg, n_alerts: int) -> Decision:
    """Is this just-closed 1-minute bar a buy for a stock that has a catalyst? (pure, testable)"""
    t = bar.t.time()
    if t < config.ALERT_START or t > NEWS_END:
        return Decision(False, "outside window")
    if market_chg is not None and market_chg <= config.MARKET_BLOCK_PCT:
        return Decision(False, "market weak")
    if n_alerts >= config.NEWS_MAX_ALERTS:
        return Decision(False, "daily cap")
    price = bar.c
    if ctx.prev_close <= 0 or price <= 0:
        return Decision(False, "bad prices")
    day = (price / ctx.prev_close - 1) * 100
    if day < MIN_CHG:
        return Decision(False, "not moving yet", day_change_pct=day)
    if day > MAX_CHG:
        return Decision(False, "already extended", day_change_pct=day)
    highs = [b.h for b in prev_bars[-15:]] if len(prev_bars) >= 5 else []
    cands = [x for x in ((max(highs) if highs else None), snap.orb_high) if x]
    if not cands:
        return Decision(False, "no range yet")
    level = max(cands)
    if price <= level:
        return Decision(False, "below range high", level=level)
    if bar.c < bar.o:
        return Decision(False, "breakout bar is red", level=level)
    zone_hi = level * (1 + min(2.0, max(config.ZONE_MIN_PCT, 0.35 * ctx.atr_pct)) / 100)
    if price > zone_hi:
        return Decision(False, "extended beyond entry zone", level=level, zone_hi=zone_hi)
    atr_pct = ctx.atr_pct if ctx.atr_pct > 0 else 2.0
    if day / atr_pct > config.MAX_EXT_ATR:
        return Decision(False, "extended vs normal daily range", level=level, zone_hi=zone_hi)
    if snap.vwap and ((price / snap.vwap - 1) * 100) / atr_pct > config.MAX_VWAP_ATR:
        return Decision(False, "stretched above VWAP", level=level)
    if snap.vwap and price <= snap.vwap:
        return Decision(False, "below VWAP", level=level)
    r = signals.rvol(snap.cum_vol, ctx.avg_vol20, bar.t)
    if r is None or r < RVOL_MIN:
        return Decision(False, "total volume low", level=level)
    if accel is None or accel < ACCEL_MIN:
        return Decision(False, "volume pace low", level=level)
    atr = ctx.atr if ctx.atr > 0 else price * 0.02
    risk = price - (level - config.SL_BELOW_PIVOT_ATR * atr)
    risk = min(max(risk, price * config.MIN_RISK_PCT / 100), config.MAX_RISK_ATR * atr)
    return Decision(True, "BUY", entry=price, sl=price - risk, t1=price + config.T1_R * risk,
                    t2=price + config.T2_R * risk, risk_pct=risk / price * 100, level=level, zone_hi=zone_hi,
                    rvol=r, day_change_pct=day, tags=[f"volume pace {accel:.1f}×", "above VWAP", "broke range high"],
                    metrics=signals.extension_metrics(price, ctx.prev_close, snap, level, atr_pct))


class CatalystEngine(Engine):
    """The live engine (ticks, 1-minute bars, state, logs) with the news rules instead of the breakout rules."""

    def __init__(self, context: dict, watch: dict, **kw):
        kw.setdefault("state_path", STATE_FILE)
        kw.setdefault("log_path", LOG_FILE)
        super().__init__(context, **kw)
        self.active, self.bars, self.cumhist = {}, {}, {}
        self.set_watch(watch)

    def set_watch(self, watch: dict):
        with self.lock:
            self.active = {s: v for s, v in watch.items() if s in self.ctx}

    def _close_bar(self, sym: str):
        st, ctx, info = self.s[sym], self.ctx[sym], self.active.get(sym)
        bar = st.bar
        if bar is None or info is None:
            return                                       # only catalyst stocks are tracked
        if (self.now_fn() - bar.t).total_seconds() > config.MAX_BAR_AGE_SECONDS:
            self.reasons["stale bar (late data)"] += 1
            return
        prev_bars = list(self.bars.setdefault(sym, deque(maxlen=20)))
        self.bars[sym].append(bar)
        hist = self.cumhist.setdefault(sym, deque(maxlen=12))
        end = bar.t + timedelta(minutes=1)
        hist.append((end, st.cum_vol))
        if sym in self.state["alerted"]:
            return
        snap = signals.Snap(bar.c, st.cum_vol, st.vwap, st.orb_high, st.orb_low, st.day_open)
        accel = radar.volume_accel(hist, end, ctx.avg_vol20)
        d = evaluate_news(ctx, bar, prev_bars, snap, accel, self.market_change_pct, len(self.state["alerted"]))
        self.reasons[d.reason] += 1
        if d.fire:
            self._fire(ctx, d, bar.t)

    def _fire(self, ctx, d, ts: datetime):
        sym, info = ctx.symbol, self.active.get(ctx.symbol, {})
        cats = " / ".join(info.get("cats", []))
        self.state["alerted"].append(sym)
        self.state["positions"][sym] = dict(entry=d.entry, sl=d.sl, sl_active=d.sl, t1=d.t1, t2=d.t2,
                                            risk=d.entry - d.sl, t1_hit=False, open=True, time=ts.strftime("%H:%M"))
        self.state.setdefault("metrics", {})[sym] = dict(d.metrics, time=ts.strftime("%H:%M"))
        self._save_state()
        self._log_event(date=ts.strftime("%Y-%m-%d"), time=ts.strftime("%H:%M:%S"), symbol=sym, tier="N",
                        event="ALERT", price=round(d.entry, 2), entry=round(d.entry, 2), sl=round(d.sl, 2),
                        t1=round(d.t1, 2), t2=round(d.t2, 2), rvol=round(d.rvol, 2),
                        score=info.get("score", ""), setup=cats)
        self.say(f"🟢 <b>BUY (News): {notify.esc(sym)}</b>  ({notify.esc(cats)})\n"
                 f"⏰ {ts.strftime('%H:%M')} · <b>₹{d.entry:,.2f}</b> ({d.day_change_pct:+.1f}% today)\n"
                 f"“{notify.esc((info.get('subject') or '')[:90])}”\n"
                 f"Entry zone ₹{d.level:,.2f}–₹{d.zone_hi:,.2f} · Stop <b>₹{d.sl:,.2f}</b> (risk {d.risk_pct:.1f}%)\n"
                 f"T1 ₹{d.t1:,.2f} · T2 ₹{d.t2:,.2f}\n{', '.join(d.tags)}")


# ---------------------------------------------------------------- universe + runner
def build_contexts(store, token_map: dict, limit: int = None):
    """Every liquid stock (so new intraday news can be picked up without re-subscribing) -> engine context."""
    feats = scoring.build_feature_table(store)
    if feats.empty:
        return None
    asof = store["date"].max()
    ok = feats[(feats["median_turnover_cr"] >= config.MIN_MEDIAN_TURNOVER_CR) & (feats["close"] >= config.MIN_PRICE)
               & ((asof - feats["date"]).dt.days <= 5) & feats["symbol"].isin(list(token_map))]
    ok = ok.sort_values("median_turnover_cr", ascending=False).head(limit or radar.MAX_SYMBOLS)
    items = [dict(symbol=r.symbol, token=token_map[r.symbol], tier="N", pivot=float(r.close), atr=float(r.atr),
                  atr_pct=float(r.atr_pct), avg_vol20=float(r.avg_vol20), prev_close=float(r.close))
             for r in ok.itertuples()]
    market = None
    px = store[store["symbol"] == config.MARKET_PROXY_SYMBOL].sort_values("date")
    if config.MARKET_PROXY_SYMBOL in token_map and not px.empty:
        market = dict(symbol=config.MARKET_PROXY_SYMBOL, token=token_map[config.MARKET_PROXY_SYMBOL],
                      prev_close=round(float(px["close"].iloc[-1]), 2))
    return dict(date=config.today_str(), items=items, market=market)


def start_refresher(engine: CatalystEngine, collect=None, every: int = REFRESH_SECONDS):
    """Background thread: pull fresh announcements every few minutes and update what the engine watches."""
    collect = collect or news.collect

    def loop():
        while True:
            time.sleep(every)
            now = config.now_ist()
            if not (dtime(9, 0) <= now.time() <= dtime(14, 35)):
                continue
            try:
                res = collect(now)
                if res.get("ok"):
                    engine.set_watch(res["watch"])
            except Exception:
                log.exception("news refresh failed")
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run(max_minutes: int = None) -> str:
    store = daily_store.load()
    if store.empty:
        store = daily_store.update()
    ctx = build_contexts(store, broker.load_token_map())
    if not ctx or not ctx["items"]:
        notify.send("❌ News engine: no stocks to watch (daily data missing).")
        return "no-universe"
    watch = news.load_watch()
    log.info("news engine: %d stocks streamed, %d with catalysts", len(ctx["items"]), len(watch))

    def factory(c):
        e = CatalystEngine(c, watch, send=lambda text, silent=False: notify.send(text, silent=silent),
                           async_send=True, now_fn=lambda: config.now_ist())
        start_refresher(e)
        return e
    now = config.now_ist()
    if now.weekday() >= 5 or now.time() >= run_day.SESSION_END:
        return "closed"
    run_day._sleep_until(run_day.CONNECT_AT)
    deadline = (config.now_ist() + timedelta(minutes=max_minutes)) if max_minutes else None
    return run_day.run_session(ctx, deadline, engine_factory=factory)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=int, default=None)
    result = run(ap.parse_args().max_minutes)
    log.info("result: %s", result)
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"resume={'true' if result == 'timeout' else 'false'}\n")
    sys.exit(0)

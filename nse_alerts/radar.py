"""Catalyst radar: tells you EARLY when a liquid NSE stock moves on surging volume AND there is
a catalyst headline behind it (results, bonus/split, merger/deal, big order, buyback, approval).

Price/volume tests first (cheap), then a news check. No catalyst headline found = no alert
(the stock is only listed in one end-of-day summary, so nothing is hidden and misses are visible).
  EARLY  (+2% to +5%): last ~5 min volume pace >=3x normal, total >=2x, beating the market by 1.5%.
  STRONG (+5% or more, e.g. an earnings gap): total volume >=3x normal.
After an EARLY alert you get ONE follow-up if the stock reaches +5%. Headlines come from Google News
RSS (best effort; it can miss or lag). INFORMATION ONLY - not a buy signal.

    python -m nse_alerts.radar [--max-minutes 345]
"""
import argparse
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, time as dtime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from . import broker, config, daily_store, engine as eng, notify, signals

log = logging.getLogger("radar")
STRONG_CHG = float(os.getenv("RADAR_MIN_CHG_PCT", "5.0"))   # % up: strong tier
MIN_RVOL = float(os.getenv("RADAR_MIN_RVOL", "3.0"))        # strong tier: x normal volume so far today
EARLY_CHG = float(os.getenv("RADAR_EARLY_CHG_PCT", "2.0"))  # % up: early tier starts here
EARLY_RVOL = float(os.getenv("RADAR_EARLY_RVOL", "2.0"))    # early tier: total volume vs normal so far
EARLY_ACCEL = float(os.getenv("RADAR_EARLY_ACCEL", "3.0"))  # early tier: last-5-min pace vs normal pace
EARLY_REL = float(os.getenv("RADAR_EARLY_REL_PCT", "1.5"))  # early tier: must beat the market by this much
REQUIRE_CATALYST = os.getenv("RADAR_REQUIRE_CATALYST", "1") != "0"   # set 0 to alert without news too
MAX_SYMBOLS = int(os.getenv("RADAR_MAX_SYMBOLS", "1200"))   # Upstox allows up to 2,000 per connection
MAX_ALERTS = int(os.getenv("RADAR_MAX_ALERTS", "25"))       # strong alerts per day
MAX_EARLY = int(os.getenv("RADAR_MAX_EARLY", "30"))         # early alerts per day
NEWS_RECHECKS, NEWS_RECHECK_WAIT, NEWS_FAIL_WAIT = 3, 600, 120   # tries / seconds between tries
STATE_FILE = os.path.join(config.DATA_DIR, "radar_state.json")
FIRST, LAST, SESSION_END = dtime(9, 16), dtime(15, 20), dtime(15, 31)
EARLY_END = dtime(14, 30)                                   # no new EARLY alerts after this
ACCEL_WINDOW_S, MIN_SPAN_S = 300, 150

# What counts as a catalyst (matched against recent headlines; case-insensitive)
CATALYSTS = [
    ("Results", r"\b(results?|earnings|quarterly|q[1-4]\s?(fy)?\s?\d{0,4}|net profit|ebitda|pat|"
                r"profit (jumps?|surges?|rises?|soars?|doubles?|climbs?|up|falls?|dips?|slumps?|declines?)|"
                r"revenue (jumps?|rises?|surges?|growth))\b"),
    ("Bonus / split", r"\b(bonus|stock split|share split|sub-?division)\b"),
    ("Merger / deal", r"\b(merger|merges?|amalgamation|acquires?|acquired|acquisition|takeover|buyout|"
                      r"demerger|open offer|joint venture|stake (sale|buy|purchase))\b"),
    ("Big order", r"\b(bags?|bagged|wins?|won|secures?|receives?|gets?|lands?)\b.{0,50}\b(orders?|contracts?)\b|"
                  r"\border (wins?|book|inflows?)\b|\bletter of (intent|award)\b|"
                  r"(rs\.?|₹)\s?[\d,\.]+\s?(crore|cr|bn|billion)\b.{0,40}\b(orders?|contracts?)\b"),
    ("Buyback", r"\bbuy-?back\b"),
    ("Approval", r"\b(usfda|fda approval|drug approval|nclt approval|cci approval)\b"),
    ("Fund raise", r"\b(qip|preferential (issue|allotment)|rights issue)\b"),
]
_CAT_RX = [(name, re.compile(rx, re.I)) for name, rx in CATALYSTS]
ALIASES = {"LT": ["L&T", "Larsen"], "M&M": ["Mahindra"], "SBIN": ["SBI", "State Bank"], "ITC": []}


def classify(title: str):
    """Catalyst category of a headline, or None."""
    for name, rx in _CAT_RX:
        if rx.search(title):
            return name
    return None


def is_relevant(symbol: str, title: str) -> bool:
    """Long tickers are distinctive; short ones (e.g. LT, ITC) must appear as a whole word."""
    if len(symbol) >= 6:
        return True
    pats = [symbol] + ALIASES.get(symbol, [])
    return any(re.search(rf"(?<![A-Za-z0-9]){re.escape(p)}(?![A-Za-z0-9])", title, re.I) for p in pats)


# ---------------------------------------------------------------- universe
def build_universe(store, token_map: dict, limit: int = MAX_SYMBOLS) -> dict:
    """{instrument_token: {symbol, prev_close, avg_vol}} for liquid, tradable, recent stocks."""
    if store.empty:
        return {}
    store = store.sort_values("date")
    asof = store["date"].max()
    last20 = store.groupby("symbol").tail(20)
    agg = last20.groupby("symbol").agg(med_turn=("turnover_cr", "median"), avg_vol=("volume", "mean"))
    last = store.groupby("symbol").tail(1).set_index("symbol")[["close", "date"]]
    df = agg.join(last)
    df = df[(df["med_turn"] >= config.MIN_MEDIAN_TURNOVER_CR) & (df["close"] >= config.MIN_PRICE)
            & ((asof - df["date"]).dt.days <= 5) & df.index.isin(list(token_map))]
    df = df.sort_values("med_turn", ascending=False).head(limit)
    return {token_map[s]: dict(symbol=s, prev_close=float(r["close"]), avg_vol=float(r["avg_vol"]))
            for s, r in df.iterrows() if r["avg_vol"] > 0}


# ---------------------------------------------------------------- headlines
def fetch_headlines(symbol: str, n: int = 8, timeout: float = 6.0, max_age_h: float = 36.0):
    """Recent (<=36h), relevant headline titles from Google News RSS. [] = searched, none found;
    None = the search itself failed (blocked / network), so the caller can retry sooner."""
    try:
        r = requests.get("https://news.google.com/rss/search",
                         params={"q": f"{symbol} NSE share", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"},
                         timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log.debug("headlines failed for %s: %s", symbol, e)
        return None
    out, now = [], datetime.now(timezone.utc)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate") or "")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = None
        if not title or dt is None or (now - dt).total_seconds() > max_age_h * 3600:
            continue                                     # undated or stale
        if is_relevant(symbol, title):
            out.append(title)
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------- radar
def volume_accel(hist, now_ts, avg_vol):
    """Volume of the last ~5 minutes vs what is normal for that stretch of the day (1.0 = normal pace)."""
    if len(hist) < 2 or avg_vol <= 0:
        return None
    old = next(((ts, c) for ts, c in hist if (now_ts - ts).total_seconds() <= ACCEL_WINDOW_S), None)
    if old is None or (now_ts - old[0]).total_seconds() < MIN_SPAN_S:
        return None
    expected = avg_vol * (signals.expected_cum_fraction(signals.minutes_since_open(now_ts))
                          - signals.expected_cum_fraction(signals.minutes_since_open(old[0])))
    return (hist[-1][1] - old[1]) / expected if expected > 0 else None


class Radar:
    def __init__(self, universe: dict, send=None, now_fn=None, headlines=None, async_send=False,
                 state_path: str = STATE_FILE, market: dict = None, require_catalyst: bool = None):
        # late-bound defaults so the real clock / sender are looked up at call time
        self.u = universe
        self.send = send or (lambda text, silent=False: notify.send(text, silent=silent))
        self.now_fn = now_fn or (lambda: config.now_ist())
        self.headlines = headlines or (lambda sym: fetch_headlines(sym))
        self.require = REQUIRE_CATALYST if require_catalyst is None else require_catalyst
        self.state_path = state_path
        self.market_token = str((market or {}).get("token", ""))
        self.market_prev = float((market or {}).get("prev_close") or 0)
        self.market_chg = None
        self.hl, self.open, self.hist, self.pending = {}, {}, {}, set()
        self.ticks, self.last_tick_wall = 0, 0.0
        self.lock = threading.RLock()
        self._q = queue.Queue() if async_send else None
        if async_send:
            for _ in range(4):                       # news lookups run in parallel, never on the feed thread
                threading.Thread(target=self._worker, daemon=True).start()
        today = self.now_fn().strftime("%Y-%m-%d")
        st = {}
        try:
            st = json.load(open(state_path)) if os.path.exists(state_path) else {}
        except Exception:
            pass
        fresh = {"date": today, "alerted": [], "early": {}, "escalated": [], "nonews": {}}
        self.state = st if st.get("date") == today and "nonews" in st else fresh

    def _save(self):
        tmp = self.state_path + ".tmp"
        json.dump(self.state, open(tmp, "w"))
        os.replace(tmp, self.state_path)

    def _may_check(self, sym: str, ts: datetime) -> bool:
        """False while a stock is waiting for its next news re-check (or has used them all)."""
        c = self.state["nonews"].get(sym)
        if not c:
            return True
        if c["n"] >= NEWS_RECHECKS:
            return False
        return (ts - datetime.fromisoformat(c["t"])).total_seconds() >= c["wait"]

    def on_tick(self, msg: dict):
        now = self.now_fn()
        t = msg if msg.get("_norm") else eng.parse_tick(msg, now)
        if not t:
            return
        ts = t["ts"]
        if ts.date() != now.date() or not (FIRST <= ts.time() <= LAST):
            return
        if t["token"] == self.market_token:
            if self.market_prev > 0:
                self.market_chg = (t["price"] / self.market_prev - 1) * 100
            return
        info = self.u.get(t["token"])
        if not info:
            return
        self.ticks += 1
        self.last_tick_wall = time.time()
        price, sym, tok = t["price"], info["symbol"], t["token"]
        payload = None
        with self.lock:
            h = self.hl.setdefault(tok, [price, price])
            h[0], h[1] = max(h[0], price), min(h[1], price)
            self.open.setdefault(tok, t.get("day_open") or price)
            cum = t.get("cum_vol")
            hist = self.hist.setdefault(tok, deque())
            if cum:
                hist.append((ts, float(cum)))
                while hist and (ts - hist[0][0]).total_seconds() > 360:
                    hist.popleft()
            chg = (price / info["prev_close"] - 1) * 100
            st = self.state
            if sym in st["early"] and sym not in st["escalated"] and chg >= STRONG_CHG:
                st["escalated"].append(sym)           # one follow-up for an EARLY name that kept running
                self._save()
                payload = dict(kind="follow", sym=sym, price=price, chg=chg, ts=ts, early_px=st["early"][sym])
            elif (sym not in st["alerted"] and sym not in self.pending and cum and chg >= EARLY_CHG
                  and self._may_check(sym, ts)):
                n_early = len(st["early"])
                n_strong = len(st["alerted"]) - n_early
                frac = max(signals.expected_cum_fraction(signals.minutes_since_open(ts)), 0.05)
                rv = cum / (info["avg_vol"] * frac)
                kind, accel = None, None
                if chg >= STRONG_CHG:
                    if rv >= MIN_RVOL and n_strong < MAX_ALERTS:
                        kind = "strong"
                elif ts.time() <= EARLY_END and n_early < MAX_EARLY and rv >= EARLY_RVOL:
                    accel = volume_accel(hist, ts, info["avg_vol"])
                    mc = self.market_chg
                    beats_market = mc is None or chg - max(mc, 0.0) >= EARLY_REL
                    if accel is not None and accel >= EARLY_ACCEL and beats_market:
                        kind = "early"
                if kind:
                    self.pending.add(sym)
                    o, vw = self.open[tok], t.get("vwap") or 0.0
                    payload = dict(
                        kind=kind, sym=sym, price=price, chg=chg, rv=rv, accel=accel, ts=ts, vwap=vw,
                        high=h[0], low=h[1], prev=info["prev_close"], mkt=self.market_chg,
                        gap=((t["day_open"] / info["prev_close"] - 1) * 100) if t.get("day_open") else None,
                        from_open=(price / o - 1) * 100 if o else None,
                        from_low=(price / h[1] - 1) * 100 if h[1] else None,
                        vs_vwap=(price / vw - 1) * 100 if vw else None)
        if payload:
            if self._q is not None:
                self._q.put(payload)
            else:
                self._emit(payload)

    def _worker(self):
        while True:
            try:
                self._emit(self._q.get())
            except Exception:
                log.exception("radar emit failed")

    def _emit(self, p: dict):
        sym = notify.esc(p["sym"])
        if p["kind"] == "follow":
            since = (p["price"] / p["early_px"] - 1) * 100
            self.send(f"📈 <b>{sym}</b> is now <b>{p['chg']:+.1f}%</b> today (₹{p['price']:,.2f}), "
                      f"<b>{since:+.1f}%</b> above its early alert price ₹{p['early_px']:,.2f}.\n"
                      f"<i>Much of the early move may already be done; chasing now carries more "
                      f"reversal risk.</i>", silent=True)
            return
        heads = self.headlines(p["sym"])                  # None = search failed, [] = nothing found
        tagged = [(classify(h), h) for h in (heads or [])]
        cats = [(c, h) for c, h in tagged if c]
        with self.lock:
            self.pending.discard(p["sym"])
            st = self.state
            if self.require and not cats:                 # no catalyst: stay silent, remember, re-check later
                failed = heads is None
                prev = st["nonews"].get(p["sym"], {})
                st["nonews"][p["sym"]] = dict(
                    t=p["ts"].isoformat(), n=prev.get("n", 0) + (0 if failed else 1),
                    wait=NEWS_FAIL_WAIT if failed else NEWS_RECHECK_WAIT, failed=failed,
                    chg=max(p["chg"], prev.get("chg", 0)), rv=p["rv"], time=p["ts"].strftime("%H:%M"))
                self._save()
                return
            if p["kind"] == "early" and len(st["early"]) >= MAX_EARLY:
                return
            st["alerted"].append(p["sym"])
            if p["kind"] == "early":
                st["early"][p["sym"]] = p["price"]
            st["nonews"].pop(p["sym"], None)
            self._save()
        early = p["kind"] == "early"
        head = (f"🟡 <b>EARLY MOVER: {sym}</b>  ({p['ts'].strftime('%H:%M')})" if early
                else f"🚀 <b>BIG MOVER: {sym}</b>  ({p['ts'].strftime('%H:%M')})")
        lines = [head,
                 f"₹{p['price']:,.2f}  (<b>{p['chg']:+.1f}%</b> today"
                 + (f", opened {p['gap']:+.1f}% vs yesterday" if p.get("gap") is not None else "") + ")"
                 + ("  — still early" if early else "")]
        if cats:
            lines.append("🏷️ Catalyst: <b>" + " / ".join(dict.fromkeys(c for c, _ in cats)) + "</b>")
        vol = f"Volume: total ≈ <b>{p['rv']:.1f}×</b> normal for this time"
        if early and p.get("accel"):
            vol = f"Volume pace (last 5 min) ≈ <b>{p['accel']:.1f}×</b> normal · total ≈ {p['rv']:.1f}×"
        lines.append(vol)
        ext = []
        if p.get("from_open") is not None:
            ext.append(f"from open {p['from_open']:+.1f}%")
        if p.get("from_low") is not None:
            ext.append(f"from day low {p['from_low']:+.1f}%")
        if p.get("vs_vwap") is not None:
            ext.append(f"vs VWAP {p['vs_vwap']:+.1f}%")
        if ext:
            lines.append("How far it has run: " + " · ".join(ext))
        if p.get("mkt") is not None:
            lines.append(f"Market proxy {p['mkt']:+.1f}%")
        if p["chg"] >= 9.5:
            lines.append("⚠️ Near a possible circuit limit: it may be hard to buy or sell.")
        shown = cats[:2] if cats else [(None, h) for h in (heads or [])[:2]]
        if shown:
            lines.append("📰 Headlines:")
            lines += [f"• {notify.esc(h)}" for _, h in shown]
        lines.append("<i>Information only, not a buy signal. Headlines come from a news search that can be "
                     "wrong or late: verify the news yourself. " + (
            "An early move can still fade: confirm with your own entry rules and skip it if it has already "
            "jumped further." if early else
            "Big moves can reverse; many traders wait for a pullback that holds above VWAP.") + "</i>")
        self.send("\n".join(lines), silent=early)

    def digest(self) -> str:
        """End-of-day summary: alerts sent, plus the movers skipped for lack of a catalyst headline."""
        with self.lock:
            st = self.state
            skipped = sorted(st["nonews"].items(), key=lambda kv: -kv[1].get("chg", 0))
        if not st["alerted"] and not skipped:
            return ""
        txt = f"📋 <b>Radar summary</b>: {len(st['alerted'])} alert(s) with a catalyst headline."
        if skipped:
            names = " · ".join(f"{notify.esc(k)} {v['chg']:+.1f}%" + (" (news check failed)" if v.get("failed") else "")
                               for k, v in skipped[:12])
            more = f" …and {len(skipped) - 12} more" if len(skipped) > 12 else ""
            txt += (f"\nMoved on volume but no catalyst headline found, so not alerted: {names}{more}\n"
                    f"<i>My news search can miss things; check these yourself if you wish.</i>")
        return txt


# ---------------------------------------------------------------- runner
def run(max_minutes: int = None) -> str:
    store = daily_store.load()
    if store.empty:
        store = daily_store.update()
    universe = build_universe(store, broker.load_token_map())
    if not universe:
        notify.send("❌ Radar: no stocks to watch (daily data missing).")
        return "no-universe"
    tmap = broker.load_token_map()
    market = None
    px = store[store["symbol"] == config.MARKET_PROXY_SYMBOL].sort_values("date")
    if config.MARKET_PROXY_SYMBOL in tmap and not px.empty:
        market = dict(token=tmap[config.MARKET_PROXY_SYMBOL], prev_close=float(px["close"].iloc[-1]))
    radar = Radar(universe, async_send=True, market=market)
    tokens, client, stream = list(universe) + ([market["token"]] if market else []), broker.make_client(), None
    now = config.now_ist()
    if now.time() < dtime(9, 0):                       # connect early, like the main monitor
        target = datetime.combine(now.date(), dtime(9, 0), tzinfo=config.IST)
        while config.now_ist() < target:
            time.sleep(min(30, max((target - config.now_ist()).total_seconds(), 1)))
    deadline = (config.now_ist() + timedelta(minutes=max_minutes)) if max_minutes else None
    gate = "only when a catalyst headline is found" if radar.require else "with or without news"
    notify.send(f"📡 <b>Catalyst radar on</b>: watching {len(universe)} liquid stocks. Alerts {gate}: "
                f"EARLY from +{EARLY_CHG:g}% (volume pace ≥{EARLY_ACCEL:g}× normal), STRONG from +{STRONG_CHG:g}%.",
                silent=True)
    failures, connected_at, zero_reconnects = 0, 0.0, 0
    while True:
        now = config.now_ist()
        t = now.time()
        if t >= SESSION_END:
            if stream:
                stream.stop()
            time.sleep(10)                              # let in-flight news checks finish
            summary = radar.digest()
            if summary:
                notify.send(summary)
            return "done"
        if deadline and now >= deadline:
            if stream:
                stream.stop()
            return "timeout"
        if stream is None or not stream.alive:
            if failures >= 8:
                notify.send("❌ Radar feed failed repeatedly - radar stopping for today.")
                return "failed"
            try:
                client.login()
                stream = broker.make_stream(client, tokens, radar.on_tick)
                stream.start()
                connected_at = time.time()
                time.sleep(5)
            except Exception as ex:
                failures += 1
                log.exception("radar connect failed")
                time.sleep(min(60, 5 * failures))
            continue
        if radar.last_tick_wall > connected_at:
            failures = 0
        if dtime(9, 20) <= t <= dtime(15, 25) and time.time() - connected_at > 60:
            if radar.ticks == 0 and t >= dtime(9, 25):
                if zero_reconnects >= 2:
                    notify.send("ℹ️ Radar: no ticks after the open (holiday or feed outage). Stopping for today.")
                    stream.stop()
                    return "holiday"
                zero_reconnects += 1
                stream.stop(); stream = None
                continue
            if radar.ticks and time.time() - radar.last_tick_wall > config.WATCHDOG_STALE_SECONDS:
                stream.stop(); stream = None
                radar.last_tick_wall = time.time()
                continue
        time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=int, default=None)
    result = run(ap.parse_args().max_minutes)
    logging.getLogger("radar").info("result: %s", result)
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"resume={'true' if result == 'timeout' else 'false'}\n")
    sys.exit(0)

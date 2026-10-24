"""Live engine: ticks in -> 1-minute bars -> signal rules -> Telegram out.

No broker code here (so it is fully testable offline). The websocket layer just
calls `engine.on_tick(msg)`; a 5-second clock calls `engine.tick_clock()`.
"""
import csv
import json
import logging
import os
import queue
import re
import threading
import time
from collections import Counter
from datetime import datetime, time as dtime
from typing import Callable, Optional

from . import config, notify, signals
from .signals import Bar, Ctx, Snap

log = logging.getLogger("engine")
MARKET_OPEN, MARKET_CLOSE = dtime(9, 15), dtime(15, 30)
ORB_END = dtime(9, 15 + config.ORB_MINUTES) if config.ORB_MINUTES < 45 else dtime(10, 0)
LOG_FIELDS = ["date", "time", "symbol", "tier", "event", "price", "entry", "sl", "t1", "t2",
              "r_multiple", "rvol", "score", "setup"]


def parse_tick(msg: dict, now: datetime) -> Optional[dict]:
    """Normalise one Angel SmartWebSocketV2 message. Prices arrive in paise."""
    try:
        tok = str(msg.get("token", "")).strip("\"' ")
        ltp = msg.get("last_traded_price")
        if not tok or not ltp:
            return None
        ms = msg.get("exchange_timestamp")
        ts = datetime.fromtimestamp(ms / 1000.0, tz=config.IST) if ms else now
        avg = msg.get("average_traded_price")
        opn = msg.get("open_price_of_the_day")
        return dict(token=tok, price=float(ltp) / 100.0, ts=ts,
                    cum_vol=msg.get("volume_trade_for_the_day"),
                    vwap=float(avg) / 100.0 if avg else 0.0,
                    day_open=float(opn) / 100.0 if opn else None)
    except Exception as e:
        log.debug("bad tick %s: %s", msg, e)
        return None


class SymState:
    __slots__ = ("price", "cum_vol", "vwap", "day_open", "orb_high", "orb_low", "bar", "headsup")

    def __init__(self):
        self.price = 0.0
        self.cum_vol = 0.0
        self.vwap = 0.0
        self.day_open = None
        self.orb_high = None
        self.orb_low = None
        self.bar: Optional[Bar] = None
        self.headsup = False


class Engine:
    def __init__(self, context: dict, send: Callable = notify.send,
                 now_fn: Callable = config.now_ist, async_send: bool = False,
                 state_path: str = None, log_path: str = None):
        self.ctx = {i["symbol"]: Ctx(**{k: i[k] for k in Ctx.__dataclass_fields__ if k in i})
                    for i in context["items"]}
        self.by_token = {c.token: s for s, c in self.ctx.items()}
        mk = context.get("market") or {}
        self.market_symbol = mk.get("symbol")
        self.market_token = str(mk.get("token", ""))
        self.market_prev_close = float(mk.get("prev_close") or 0)
        self.market_change_pct: Optional[float] = None
        self.s = {sym: SymState() for sym in self.ctx}
        self.now_fn = now_fn
        self.state_path = state_path or config.STATE_FILE
        self.log_path = log_path or config.ALERT_LOG
        self.lock = threading.RLock()
        self.reasons = Counter()
        self.ticks = 0
        self.last_tick_wall = 0.0
        self._send_fn = send
        self._q: Optional[queue.Queue] = None
        if async_send:
            self._q = queue.Queue()
            threading.Thread(target=self._sender, daemon=True).start()
        self._load_state()

    # ---------------- outbound messages ----------------
    def _sender(self):
        while True:
            text, silent = self._q.get()
            try:
                self._send_fn(text, silent=silent)
            except Exception as e:
                log.warning("send failed: %s", e)

    def say(self, text: str, silent: bool = False):
        if self._q is not None:
            self._q.put((text, silent))
        else:
            self._send_fn(text, silent=silent)

    # ---------------- persistence ----------------
    def _load_state(self):
        today = self.now_fn().strftime("%Y-%m-%d")
        st = {}
        try:
            if os.path.exists(self.state_path):
                st = json.load(open(self.state_path))
        except Exception as e:
            log.warning("state unreadable: %s", e)
        if st.get("date") != today:
            st = {"date": today, "alerted": [], "headsup": [], "b_count": 0, "positions": {}}
        self.state = st
        for sym in st["headsup"]:
            if sym in self.s:
                self.s[sym].headsup = True

    def _save_state(self):
        tmp = self.state_path + ".tmp"
        json.dump(self.state, open(tmp, "w"), indent=1)
        os.replace(tmp, self.state_path)

    def _log_event(self, **row):
        new = not os.path.exists(self.log_path)
        with open(self.log_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in LOG_FIELDS})

    # ---------------- tick path ----------------
    def on_tick(self, msg: dict):
        now = self.now_fn()
        t = msg if msg.get("_norm") else parse_tick(msg, now)   # Upstox adapter pre-normalises
        if not t:
            return
        if not self._in_session(t["ts"], now):
            return              # stale / pre-open snapshots sent on subscribe don't count as liveness
        self.last_tick_wall = time.time()
        self.ticks += 1
        with self.lock:
            if t["token"] == self.market_token:
                if self.market_prev_close > 0:
                    self.market_change_pct = (t["price"] / self.market_prev_close - 1) * 100
                return
            sym = self.by_token.get(t["token"])
            if not sym:
                return
            ctx, st, ts, price = self.ctx[sym], self.s[sym], t["ts"], t["price"]
            st.price = price
            if t["cum_vol"] is not None:
                st.cum_vol = float(t["cum_vol"])
            if t["vwap"]:
                st.vwap = t["vwap"]
            if st.day_open is None:
                st.day_open = t["day_open"] or price
            if ts.time() < ORB_END:
                st.orb_high = price if st.orb_high is None else max(st.orb_high, price)
                st.orb_low = price if st.orb_low is None else min(st.orb_low, price)
            minute = ts.replace(second=0, microsecond=0)
            if st.bar is None:
                st.bar = Bar(minute, price, price, price, price)
            elif minute > st.bar.t:
                self._close_bar(sym)
                st.bar = Bar(minute, price, price, price, price)
            elif minute == st.bar.t:
                b = st.bar
                b.h, b.l, b.c = max(b.h, price), min(b.l, price), price
            self._check_position(sym, price, ts)
            if ctx.tier == "A" and not st.headsup and sym not in self.state["alerted"]:
                snap = Snap(price, st.cum_vol, st.vwap, st.orb_high, st.orb_low, st.day_open)
                if signals.heads_up(ctx, snap, ts, self.market_change_pct):
                    st.headsup = True
                    self.state["headsup"].append(sym)
                    self._save_state()
                    lvl = max(ctx.pivot, st.orb_high or 0)
                    self.say(f"👀 <b>{notify.esc(sym)}</b> is within 0.3% of breaking out "
                             f"(₹{price:,.2f} vs level ₹{lvl:,.2f}), volume building. "
                             f"Not a buy signal yet.", silent=True)

    @staticmethod
    def _in_session(ts: datetime, now: datetime) -> bool:
        return ts.date() == now.date() and MARKET_OPEN <= ts.time() <= MARKET_CLOSE

    def tick_clock(self):
        """Close bars whose minute has passed even if no newer tick arrived."""
        now = self.now_fn()
        cur = now.replace(second=0, microsecond=0)
        with self.lock:
            for sym, st in self.s.items():
                if st.bar is not None and st.bar.t < cur and now.second >= 2:
                    self._close_bar(sym)
                    st.bar = None

    def _close_bar(self, sym: str):
        st, ctx = self.s[sym], self.ctx[sym]
        bar = st.bar
        if bar is None or sym in self.state["alerted"]:
            return
        if (self.now_fn() - bar.t).total_seconds() > config.MAX_BAR_AGE_SECONDS:
            self.reasons["stale bar (late data)"] += 1     # backlog after reconnect/slow start
            return
        snap = Snap(bar.c, st.cum_vol, st.vwap, st.orb_high, st.orb_low, st.day_open)
        d = signals.evaluate(ctx, bar, snap, self.market_change_pct,
                             len(self.state["alerted"]), self.state["b_count"])
        self.reasons[re.sub(r"[-+]?\d+(\.\d+)?%?", "#", d.reason)] += 1
        if d.fire:
            self._fire(ctx, d, bar.t)

    # ---------------- alert + trade tracking ----------------
    def _fire(self, ctx: Ctx, d, ts: datetime):
        sym = ctx.symbol
        self.state["alerted"].append(sym)
        if ctx.tier == "B":
            self.state["b_count"] += 1
        risk = d.entry - d.sl
        self.state["positions"][sym] = dict(entry=d.entry, sl=d.sl, sl_active=d.sl, t1=d.t1, t2=d.t2,
                                            risk=risk, t1_hit=False, open=True,
                                            time=ts.strftime("%H:%M"))
        self._save_state()
        self._log_event(date=ts.strftime("%Y-%m-%d"), time=ts.strftime("%H:%M:%S"), symbol=sym,
                        tier=ctx.tier, event="ALERT", price=round(d.entry, 2), entry=round(d.entry, 2),
                        sl=round(d.sl, 2), t1=round(d.t1, 2), t2=round(d.t2, 2),
                        rvol=round(d.rvol, 2), score=ctx.score, setup=ctx.setup)
        mk = f"{self.market_change_pct:+.2f}%" if self.market_change_pct is not None else "n/a"
        self.say(
            f"🟢 <b>BUY ALERT: {notify.esc(sym)}</b>  ({'A' if ctx.tier == 'A' else 'B'}-list)\n"
            f"⏰ {ts.strftime('%H:%M')}  |  Price <b>₹{d.entry:,.2f}</b> ({d.day_change_pct:+.1f}% today)\n"
            f"📈 Broke ₹{d.level:,.2f} — entry zone ₹{d.level:,.2f} to ₹{d.zone_hi:,.2f}\n"
            f"🛑 Stop <b>₹{d.sl:,.2f}</b> (risk {d.risk_pct:.1f}%)\n"
            f"🎯 T1 ₹{d.t1:,.2f} ({config.T1_R:g}R)   T2 ₹{d.t2:,.2f} ({config.T2_R:g}R)\n"
            f"📊 {', '.join(d.tags)}\n"
            f"🧭 Setup: {notify.esc(ctx.setup)} · score {ctx.score:.0f} · RS rank {ctx.rs_rank:.0f}\n"
            f"🌐 Nifty proxy {mk}\n"
            f"<i>If price is already above ₹{d.zone_hi:,.2f} when you read this, skip it. "
            f"After T1, move stop to entry. Not financial advice.</i>")

    def _check_position(self, sym: str, price: float, ts: datetime):
        p = self.state["positions"].get(sym)
        if not p or not p["open"]:
            return
        risk, tier = p["risk"], self.ctx[sym].tier
        if price <= p["sl_active"]:
            r = (p["sl_active"] - p["entry"]) / risk
            p["open"] = False
            ev, txt = ("BE_STOP", "stopped at breakeven after T1") if p["t1_hit"] else ("SL", "stop-loss hit")
            self._close_pos(sym, tier, ev, p["sl_active"], r, ts, f"🔴 {notify.esc(sym)}: {txt} (₹{price:,.2f}, {r:+.2f}R)")
        elif price >= p["t2"]:
            p["open"] = False
            self._close_pos(sym, tier, "T2", p["t2"], config.T2_R, ts,
                            f"🏆 {notify.esc(sym)}: Target 2 hit (₹{price:,.2f}, +{config.T2_R:g}R). Book the rest.")
        elif price >= p["t1"] and not p["t1_hit"]:
            p["t1_hit"] = True
            p["sl_active"] = p["entry"]
            self._close_pos(sym, tier, "T1", p["t1"], config.T1_R, ts,
                            f"🎯 {notify.esc(sym)}: Target 1 hit (₹{price:,.2f}, +{config.T1_R:g}R). "
                            f"Book partial; move stop to entry ₹{p['entry']:,.2f}.", closing=False)

    def _close_pos(self, sym, tier, event, px, r, ts, text, closing=True):
        self._save_state()
        self._log_event(date=ts.strftime("%Y-%m-%d"), time=ts.strftime("%H:%M:%S"), symbol=sym,
                        tier=tier, event=event, price=round(px, 2), r_multiple=round(r, 2))
        self.say(text)

    # ---------------- end of day ----------------
    def end_of_day(self):
        with self.lock:
            now = self.now_fn()
            total_r, lines = 0.0, []
            for sym, p in self.state["positions"].items():
                if p["open"]:
                    px = self.s[sym].price or p["entry"]
                    r = (px - p["entry"]) / p["risk"]
                    p["open"] = False
                    self._log_event(date=now.strftime("%Y-%m-%d"), time=now.strftime("%H:%M:%S"),
                                    symbol=sym, tier=self.ctx[sym].tier, event="EOD",
                                    price=round(px, 2), r_multiple=round(r, 2))
                    p["final_r"] = r
                else:
                    p.setdefault("final_r", None)
            rows = self._outcomes()
            for sym, r, tag in rows:
                total_r += r
                lines.append(f"• {notify.esc(sym)}: {r:+.2f}R ({tag})")
            self._save_state()
            n = len(self.state["alerted"])
            body = "\n".join(lines) if lines else "No alerts today."
            self.say(f"📋 <b>End of day {now.strftime('%d %b')}</b>: {n} alert(s), net {total_r:+.2f}R\n{body}\n"
                     f"<i>Ticks processed: {self.ticks:,}. Top skip reasons: "
                     f"{', '.join(f'{k} {v}' for k, v in self.reasons.most_common(4))}</i>")

    def _outcomes(self):
        out = []
        if not os.path.exists(self.log_path):
            return out
        today = self.now_fn().strftime("%Y-%m-%d")
        last = {}
        for row in csv.DictReader(open(self.log_path)):
            if row["date"] == today and row["event"] in ("SL", "BE_STOP", "T2", "EOD"):
                last[row["symbol"]] = (float(row["r_multiple"] or 0), row["event"])
        t1_only = {r["symbol"] for r in csv.DictReader(open(self.log_path))
                   if r["date"] == today and r["event"] == "T1"}
        for sym, (r, ev) in last.items():
            out.append((sym, r, ev + (" after T1" if sym in t1_only and ev == "EOD" else "")))
        return out

    # ---------------- optional bootstrap after a mid-day restart ----------------
    def bootstrap_orb(self, client, only_tier="A"):
        """Rebuild opening range for A-list from 1-minute candles (needs broker REST)."""
        now = self.now_fn()
        if now.time() <= ORB_END:
            return
        day = now.strftime("%Y-%m-%d")
        for sym, ctx in self.ctx.items():
            if ctx.tier != only_tier:
                continue
            try:
                rows = client.candles(ctx.token, "ONE_MINUTE", f"{day} 09:15", f"{day} {ORB_END.strftime('%H:%M')}")
                if rows:
                    with self.lock:
                        st = self.s[sym]
                        st.orb_high = max(r[2] for r in rows)
                        st.orb_low = min(r[3] for r in rows)
                        st.day_open = st.day_open or rows[0][1]
            except Exception as e:
                log.warning("ORB bootstrap failed for %s: %s", sym, e)

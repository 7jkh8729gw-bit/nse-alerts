"""After-close summary + record of every alert (for improving the engines).

Runs once after the close (workflow action 'report', ~16:00). For each alert of the day, from BOTH engines
(breakout and news), it checks what the price did afterwards (Upstox 1-minute candles) using the SAME rules
as live trading, appends one row per alert to results/alert_results.csv, and sends ONE simple message:

    📊 Day summary · Tue 10 Nov
    Breakout: 3 alerts · 1 target hit · 1 stop-loss · 1 neither (+0.4R)
    News: 2 alerts · 0 target hit · 1 stop-loss · 1 neither (-0.6R)
    Total: 5 alerts · 1 target hit · 2 stop-loss · net -0.2R

Rules: stop sits under the breakout level; T1 = 1.5R (stop then moves to entry); T2 = 3R. A candle touching
both stop and target counts as a STOP (cautious). On Fridays one extra line gives the running totals and the
cumulative CSV is attached: keep it.

    python -m nse_alerts.outcomes [--date YYYY-MM-DD] [--weekly] [--no-send]
"""
import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime

import requests

from . import broker, config

log = logging.getLogger("outcomes")
RESULTS_DIR = os.getenv("RESULTS_DIR", "results")
RESULTS = os.path.join(RESULTS_DIR, "alert_results.csv")
SOURCES = [("breakout", config.ALERT_LOG), ("news", os.path.join(config.DATA_DIR, "catalyst_log.csv"))]
STATES = {"breakout": config.STATE_FILE, "news": os.path.join(config.DATA_DIR, "catalyst_state.json")}
FIELDS = ["date", "time", "system", "symbol", "tier", "setup", "score", "rvol", "entry", "sl", "t1", "t2",
          "outcome", "r", "mfe_r", "mae_r", "minutes_to_outcome", "close_r",
          "day_chg_pct", "atr_pct", "ext_atr", "vwap_atr", "above_level_pct", "from_open_pct"]
METRIC_KEYS = ["day_chg_pct", "atr_pct", "ext_atr", "vwap_atr", "above_level_pct", "from_open_pct"]
TARGET_HIT = {"T2", "EOD_after_T1", "BE_STOP"}          # T1 was reached


# ------------------------------------------------------------------ candle maths (pure)
def _m(ts: str) -> int:
    """Minute-of-day from a candle timestamp like '2026-11-10T09:15:00+05:30'."""
    return int(ts[11:13]) * 60 + int(ts[14:16])


def _hm(hhmm: str) -> int:
    return int(hhmm[0:2]) * 60 + int(hhmm[3:5])


def engine_outcome(rows: list, alert_hhmm: str, entry: float, sl: float, t1: float, t2: float) -> dict:
    """What an alert did, using the live trade rules. rows = [ts, o, h, l, c, v] oldest first."""
    risk = entry - sl
    later = [r for r in rows if _m(r[0]) > _hm(alert_hhmm)]
    if risk <= 0 or not later:
        return dict(outcome="NO_DATA", r="", mfe_r="", mae_r="", minutes_to_outcome="", close_r="")
    R = lambda p: (p - entry) / risk
    stop, t1_hit, mfe, mae = sl, False, 0.0, 0.0
    start = _m(later[0][0])
    close_r = round(R(float(later[-1][4])), 2)
    for c in later:
        hi, lo = float(c[2]), float(c[3])
        mfe, mae = max(mfe, R(hi)), min(mae, R(lo))
        mins = _m(c[0]) - start + 1
        if lo <= stop:                                   # checked first: a candle touching both = stop (cautious)
            return dict(outcome="BE_STOP" if t1_hit else "SL", r=round(R(stop), 2), mfe_r=round(mfe, 2),
                        mae_r=round(mae, 2), minutes_to_outcome=mins, close_r=close_r)
        if hi >= t2:
            return dict(outcome="T2", r=round(R(t2), 2), mfe_r=round(mfe, 2), mae_r=round(mae, 2),
                        minutes_to_outcome=mins, close_r=close_r)
        if hi >= t1 and not t1_hit:
            t1_hit, stop = True, entry                   # live rule: move the stop to entry after T1
    return dict(outcome="EOD_after_T1" if t1_hit else "EOD", r=close_r, mfe_r=round(mfe, 2), mae_r=round(mae, 2),
                minutes_to_outcome=_m(later[-1][0]) - start + 1, close_r=close_r)


# ------------------------------------------------------------------ inputs / storage
def load_alerts(day: str) -> list:
    out = []
    for system, path in SOURCES:
        if os.path.exists(path):
            for r in csv.DictReader(open(path)):
                if r.get("date") == day and r.get("event") == "ALERT":
                    out.append(dict(r, system=system))
    return out


def load_metrics(day: str) -> dict:
    """{(system, symbol): how extended the entry was} from today's engine state files."""
    out = {}
    for system, path in STATES.items():
        try:
            st = json.load(open(path))
        except Exception:
            continue
        if st.get("date") == day:
            for sym, m in (st.get("metrics") or {}).items():
                out[(system, sym)] = m
    return out


def _merge(rows_new: list) -> list:
    """Append rows, replacing any row with the same key (re-running a day is safe)."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    key = lambda r: (r["date"], r["system"], r["symbol"], r["time"])
    rows = {key(r): r for r in (list(csv.DictReader(open(RESULTS))) if os.path.exists(RESULTS) else [])}
    for r in rows_new:
        rows[key(r)] = {k: r.get(k, "") for k in FIELDS}
    ordered = sorted(rows.values(), key=lambda r: (r["date"], r["time"], r["symbol"]))
    with open(RESULTS, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(ordered)
    return ordered


# ------------------------------------------------------------------ summary text
def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def tally(rows: list) -> dict:
    t = dict(n=len(rows), target=0, stop=0, neither=0, unknown=0, net_r=0.0)
    for r in rows:
        o = r.get("outcome", "")
        t["target"] += o in TARGET_HIT
        t["stop"] += o == "SL"
        t["neither"] += o == "EOD"
        t["unknown"] += o in ("NO_DATA", "")
        t["net_r"] += _f(r.get("r")) or 0.0
    return t


def _line(label: str, t: dict) -> str:
    if not t["n"]:
        return f"{label}: no alerts"
    s = (f"{label}: {t['n']} alert{'s' if t['n'] != 1 else ''} · {t['target']} target hit · {t['stop']} stop-loss"
         f" · {t['neither']} neither ({t['net_r']:+.1f}R)")
    return s + (f" · {t['unknown']} no data" if t["unknown"] else "")


def daily_text(day: str, rows: list) -> str:
    d = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b")
    if not rows:
        return f"📊 <b>Day summary · {d}</b>\nNo alerts today."
    lines = [f"📊 <b>Day summary · {d}</b>"]
    for system, label in (("breakout", "Breakout"), ("news", "News")):
        lines.append(_line(label, tally([r for r in rows if r["system"] == system])))
    tot = tally(rows)
    lines.append(f"Total: {tot['n']} alert{'s' if tot['n'] != 1 else ''} · {tot['target']} target hit · "
                 f"{tot['stop']} stop-loss · net {tot['net_r']:+.1f}R")
    return "\n".join(lines)


def running_text(all_rows: list) -> str:
    days = len({r["date"] for r in all_rows})
    parts = []
    for system, label in (("breakout", "Breakout"), ("news", "News")):
        sub = [r for r in all_rows if r["system"] == system and _f(r.get("r")) is not None]
        if sub:
            win = sum(_f(r["r"]) > 0 for r in sub) / len(sub) * 100
            parts.append(f"{label} {len(sub)} alert{'s' if len(sub) != 1 else ''}, {win:.0f}% positive, "
                         f"avg {sum(_f(r['r']) for r in sub) / len(sub):+.2f}R")
    return f"Since start ({days} day{'s' if days != 1 else ''}): " + (" · ".join(parts) if parts else "no results yet")


def extension_text(all_rows: list, min_rows: int = 6) -> str:
    """Do alerts that had already run further do worse? Grouped by day's gain / the stock's normal daily range."""
    rows = [(_f(r.get("ext_atr")), _f(r.get("r"))) for r in all_rows]
    rows = [(e, r) for e, r in rows if e is not None and r is not None]
    if len(rows) < min_rows:
        return f"Extension check: collecting data ({len(rows)} alerts measured so far)"
    buckets = [("under 0.5", 0, 0.5), ("0.5-0.9", 0.5, 0.9), ("0.9 and over", 0.9, 99)]
    parts = []
    for name, lo, hi in buckets:
        sub = [r for e, r in rows if lo <= e < hi]
        if sub:
            parts.append(f"{name}: {len(sub)} alert{'s' if len(sub) != 1 else ''}, avg {sum(sub) / len(sub):+.2f}R")
    return "By how far each stock had already run (day gain ÷ its normal daily range): " + " · ".join(parts)


# ------------------------------------------------------------------ telegram + main
def send_document(path: str, caption: str = "") -> bool:
    if not config.BOT_TOKEN or not config.CHAT_ID or not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendDocument",
                              data={"chat_id": config.CHAT_ID, "caption": caption[:900]},
                              files={"document": (os.path.basename(path), f)}, timeout=60)
        return r.status_code == 200
    except Exception as e:
        log.warning("sendDocument failed: %s", e)
        return False


def run(day: str = None, weekly: bool = None, send: bool = True, client=None) -> dict:
    from . import notify
    day = day or config.today_str()
    alerts = load_alerts(day)
    candles = {}
    symbols = {a["symbol"] for a in alerts}
    if symbols:
        client = client or broker.make_client()
        client.login()
        tmap = broker.load_token_map()
        for sym in sorted(symbols):
            candles[sym] = client.candles(tmap[sym], "ONE_MINUTE", f"{day} 09:15", f"{day} 15:30") if sym in tmap else []
    metrics = load_metrics(day)
    rows = []
    for a in alerts:
        res = engine_outcome(candles.get(a["symbol"], []), a["time"][:5], float(a["entry"]), float(a["sl"]),
                             float(a["t1"]), float(a["t2"]))
        rows.append(dict(date=day, time=a["time"][:5], system=a["system"], symbol=a["symbol"], tier=a.get("tier", ""),
                         setup=a.get("setup", ""), score=a.get("score", ""), rvol=a.get("rvol", ""), entry=a["entry"],
                         sl=a["sl"], t1=a["t1"], t2=a["t2"], **res,
                         **{k: metrics.get((a["system"], a["symbol"]), {}).get(k, "") for k in METRIC_KEYS}))
    all_rows = _merge(rows)
    text = daily_text(day, rows)
    is_weekly = (datetime.strptime(day, "%Y-%m-%d").weekday() == 4) if weekly is None else weekly
    if is_weekly:
        text += "\n" + running_text(all_rows) + "\n" + extension_text(all_rows)
    if send:
        notify.send(text)
        if is_weekly:
            send_document(RESULTS, "alert_results.csv (every alert so far: keep this file)")
    return dict(text=text, rows=rows, weekly=is_weekly)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--weekly", action="store_true")
    ap.add_argument("--no-send", action="store_true")
    a = ap.parse_args()
    out = run(a.date, True if a.weekly else None, send=not a.no_send)
    print(out["text"])
    sys.exit(0)

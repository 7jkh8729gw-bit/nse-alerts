"""Pre-market job (~08:10 IST): update data -> screen whole NSE -> write today's context.
Usage:  python -m nse_alerts.premarket [--seed-days 700]"""
import argparse
import json
import logging
import os
from datetime import timedelta

import pandas as pd

from . import broker, config, daily_store, notify, scoring

log = logging.getLogger("premarket")


def prev_weekday(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def build_context(store: pd.DataFrame, token_map: dict) -> dict:
    liquid, scored = scoring.screen(store)
    scored = scored[scored["symbol"].isin(token_map)].reset_index(drop=True)
    top = scored.head(max(1, config.MAX_SUBSCRIPTIONS - 1))
    items = []
    for i, r in top.iterrows():
        items.append(dict(
            symbol=r["symbol"], token=token_map[r["symbol"]], tier="A" if i < config.A_LIST_SIZE else "B",
            pivot=round(float(r["pivot"]), 2), atr=round(float(r["atr"]), 3),
            atr_pct=round(float(r["atr_pct"]), 2), avg_vol20=float(r["avg_vol20"]),
            prev_close=round(float(r["close"]), 2), score=float(r["score"]), setup=r["setup"],
            rs_rank=round(float(r["rs_rank"]), 1), dist_pivot_pct=round(float(r["dist_pivot_pct"]), 2)))
    market = None
    proxy = config.MARKET_PROXY_SYMBOL
    px = store[store["symbol"] == proxy].sort_values("date")
    if proxy in token_map and not px.empty:
        market = dict(symbol=proxy, token=token_map[proxy], prev_close=round(float(px["close"].iloc[-1]), 2))
    return dict(date=config.today_str(), built_at=config.now_ist().isoformat(timespec="seconds"),
                last_session=str(store["date"].max().date()), liquid=liquid, gated=int(len(scored)),
                market=market, items=items)


def message(ctx: dict) -> str:
    a = [i for i in ctx["items"] if i["tier"] == "A"]
    n_b = len(ctx["items"]) - len(a)
    lines = [f"🌅 <b>Pre-market watchlist · {config.now_ist().strftime('%a %d %b')}</b>",
             f"Scanned whole NSE → {ctx['liquid']:,} liquid stocks → <b>{ctx['gated']}</b> in uptrend, "
             f"near a breakout level.",
             f"<b>{len(a)} A-list</b> (ranked) + {n_b} B-list are watched live from 09:15 "
             f"(alerts from 09:30). Data as of {ctx['last_session']}.", ""]
    for n, i in enumerate(a[:15], 1):
        lines.append(f"{n}. <b>{notify.esc(i['symbol'])}</b> ₹{i['prev_close']:,.2f} → trigger "
                     f"₹{i['pivot']:,.2f} (+{i['dist_pivot_pct']:.1f}%) · {i['score']:.0f} · {notify.esc(i['setup'])}")
    if len(a) > 15:
        lines.append(f"…and {len(a) - 15} more A-list names.")
    return "\n".join(lines)


def run(seed_days: int = None) -> dict:
    store = daily_store.update(seed_days)
    if store.empty:
        raise RuntimeError("Daily store is empty - bhavcopy download failed (see log).")
    last = store["date"].max().date()
    expected = prev_weekday(config.now_ist().date())
    if last < expected:
        notify.send(f"⚠️ Latest NSE data is {last}, expected {expected} (holiday, or file not yet published). "
                    f"Watchlist uses older data.")
    ctx = build_context(store, broker.load_token_map())
    tmp = config.CONTEXT_FILE + ".tmp"
    json.dump(ctx, open(tmp, "w"), indent=1)
    os.replace(tmp, config.CONTEXT_FILE)
    notify.send(message(ctx))
    log.info("Context written: %d items (%d gated)", len(ctx["items"]), ctx["gated"])
    return ctx


def load_today() -> dict | None:
    try:
        ctx = json.load(open(config.CONTEXT_FILE))
        return ctx if ctx.get("date") == config.today_str() and ctx.get("items") else None
    except Exception:
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-days", type=int, default=None,
                    help="(re)download this many calendar days of history (use ~700 for backtests)")
    run(ap.parse_args().seed_days)

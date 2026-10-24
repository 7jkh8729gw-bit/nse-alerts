"""Daily supervisor.  One always-on process does everything:

  08:10  pre-market scan of the whole NSE -> today's context + Telegram watchlist
  09:00  login + websocket subscribe (so the feed is warm before the open)
  09:15-15:30  live engine; watchdog auto-reconnects (re-login) if ticks stall
  15:31  end-of-day summary, then sleep until the next trading morning

   python -m nse_alerts.run_day --daemon        # VPS / home PC / Raspberry Pi (recommended)
   python -m nse_alerts.run_day --once --max-minutes 350   # GitHub Actions fallback
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta

from . import broker, config, engine as eng, notify, premarket, upstox

log = logging.getLogger("run_day")
PREMARKET_AT, CONNECT_AT, SESSION_END = dtime(8, 10), dtime(9, 0), dtime(15, 31)


def _sleep_until(t: dtime, day_offset: int = 0):
    now = config.now_ist()
    target = datetime.combine(now.date() + timedelta(days=day_offset), t, tzinfo=config.IST)
    while True:
        left = (target - config.now_ist()).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 30))


def _next_trading_morning_offset(now) -> int:
    d = 1
    while (now + timedelta(days=d)).weekday() >= 5:
        d += 1
    return d


def run_session(ctx: dict, deadline: datetime = None) -> str:
    """Returns 'done' | 'timeout' | 'holiday' | 'failed'."""
    e = eng.Engine(ctx, send=lambda text, silent=False: notify.send(text, silent=silent),
                   async_send=True, now_fn=lambda: config.now_ist())
    tokens = [i["token"] for i in ctx["items"]]
    if ctx.get("market"):
        tokens.append(str(ctx["market"]["token"]))
    client, stream = broker.make_client(), None
    failures, connected_at, last_warn = 0, 0.0, 0.0
    note = ""
    if config.BROKER == "upstox":
        left = upstox.token_days_left(config.UPSTOX_ANALYTICS_TOKEN)
        if left is not None and left <= 14:
            note = f"\n⚠️ Your Upstox Analytics Token expires in {left} day(s): generate a new one and update .env."
    notify.send(f"✅ <b>Live monitor started</b> · watching {len(ctx['items'])} stocks "
                f"({sum(1 for i in ctx['items'] if i['tier'] == 'A')} A-list). "
                f"Alerts 09:30–{config.ALERT_END.strftime('%H:%M')}.{note}", silent=True)
    boot_done = False
    zero_tick_reconnects = 0

    def warn(text, silent=False):
        nonlocal last_warn
        if time.time() - last_warn > 600:        # at most one warning per 10 minutes
            notify.send(text, silent=silent)
            last_warn = time.time()

    while True:
        now = config.now_ist()
        t = now.time()
        if t >= SESSION_END:
            e.end_of_day()
            time.sleep(8)                          # let the sender thread flush
            if stream:
                stream.stop()
            return "done"
        if deadline and now >= deadline:
            if stream:
                stream.stop()
            return "timeout"

        if stream is None or not stream.alive:     # (re)connect
            if failures >= 8:
                notify.send("❌ Live feed failed 8 times in a row - monitor giving up for today.")
                return "failed"
            try:
                client.login()
                stream = broker.make_stream(client, tokens, e.on_tick)
                stream.start()
                connected_at = time.time()
                time.sleep(5)
                if not boot_done and t > dtime(9, 31):
                    e.bootstrap_orb(client)
                    boot_done = True
            except Exception as ex:
                failures += 1
                log.exception("connect failed")
                warn(f"⚠️ Feed connect problem: {notify.esc(ex)} (retrying)")
                time.sleep(min(60, 5 * failures))
            continue

        e.tick_clock()
        if e.last_tick_wall > connected_at:        # data is flowing -> forgive earlier failures
            failures = 0
        settled = time.time() - connected_at > 60  # give a fresh connection a minute
        if dtime(9, 16) <= t <= dtime(15, 29) and settled:
            if e.ticks == 0 and t >= dtime(9, 22):  # nothing at all since the open
                if zero_tick_reconnects >= 2:
                    notify.send("ℹ️ No market ticks received after the open - NSE holiday or feed outage. "
                                "Monitor stopping for today.")
                    stream.stop()
                    return "holiday"
                zero_tick_reconnects += 1
                stream.stop(); stream = None
                continue
            idle = time.time() - e.last_tick_wall if e.ticks else 0
            if idle > config.WATCHDOG_STALE_SECONDS:
                log.warning("No ticks for %.0fs - reconnecting", idle)
                warn("⚠️ Feed stalled - reconnecting.", silent=True)
                stream.stop(); stream = None
                e.last_tick_wall = time.time()
                continue
        time.sleep(5)


def ensure_context():
    ctx = premarket.load_today()
    if ctx:
        return ctx
    for attempt in range(3):
        try:
            return premarket.run()
        except Exception as ex:
            log.exception("premarket failed")
            notify.send(f"❌ Pre-market scan failed (attempt {attempt + 1}/3): {notify.esc(ex)}")
            time.sleep(300)
    return None


def day(max_minutes: int = None) -> str:
    now = config.now_ist()
    if now.weekday() >= 5:
        return "weekend"
    if now.time() >= SESSION_END:
        return "after-hours"
    ctx = ensure_context()
    if not ctx:
        return "no-context"
    _sleep_until(CONNECT_AT)
    deadline = (config.now_ist() + timedelta(minutes=max_minutes)) if max_minutes else None
    return run_session(ctx, deadline)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--max-minutes", type=int, default=None)
    a = ap.parse_args()
    if a.daemon:
        notify.send("🤖 NSE alert daemon started.", silent=True)
        done_date = None
        while True:
            now = config.now_ist()
            if now.weekday() < 5 and now.time() < SESSION_END and done_date != now.date():
                if now.time() < PREMARKET_AT:
                    _sleep_until(PREMARKET_AT)
                try:
                    log.info("day result: %s", day())
                    done_date = config.now_ist().date()     # one run per trading day
                except Exception:
                    log.exception("day crashed")
                    notify.send("❌ Daily run crashed - restarting it (state is restored, no duplicate alerts).")
                    time.sleep(60)
            else:
                _sleep_until(PREMARKET_AT, _next_trading_morning_offset(now))
    else:
        result = day(a.max_minutes)
        log.info("result: %s", result)
        out = os.getenv("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write(f"resume={'true' if result == 'timeout' else 'false'}\n")
        sys.exit(0)


if __name__ == "__main__":
    main()

"""Step-by-step connectivity check. Run once after setup:  python -m nse_alerts.selftest"""
import logging
import time
from datetime import timedelta

from . import broker, config, daily_store, engine, notify, upstox


def step(name, fn):
    try:
        out = fn()
        print(f"[ OK ] {name}" + (f" - {out}" if out else ""))
        return True
    except Exception as e:
        print(f"[FAIL] {name}: {e}")
        return False


def main():
    logging.basicConfig(level=logging.WARNING)
    print(f"Broker: {config.BROKER}\n")

    def tg():
        if not notify.send("🧪 Self-test: Telegram works."):
            raise RuntimeError("check BOT_TOKEN / CHAT_ID")
    step("Telegram message", tg)

    d = config.now_ist().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)

    def bhav():
        for back in range(0, 6):
            status, df = daily_store.fetch_day(d - timedelta(days=back))
            if status == "ok":
                return f"{len(df)} EQ stocks on {df['date'].iloc[0].date()}"
        raise RuntimeError("could not download any recent bhavcopy (NSE blocked this network?)")
    step("NSE end-of-day file download", bhav)
    step("Instrument list", lambda: f"{len(broker.load_token_map(True))} NSE equities")
    client = broker.make_client()

    def login():
        client.login()
        if config.BROKER == "upstox":
            left = upstox.token_days_left(client.token)
            return f"token valid, {left} days left" if left is not None else "token accepted"
    if not step("Broker login / token check", login):
        return
    tmap = broker.load_token_map()
    toks = [tmap[s] for s in ("RELIANCE", "TCS", config.MARKET_PROXY_SYMBOL) if s in tmap]

    def hist():
        day = d.strftime("%Y-%m-%d")
        rows = client.candles(tmap["RELIANCE"], "FIVE_MINUTE", f"{day} 09:15", f"{day} 15:30")
        if not rows:
            raise RuntimeError("no candles returned")
        return f"{len(rows)} five-minute candles for RELIANCE on {day}"
    step("Historical candles", hist)

    got = []
    s = broker.make_stream(client, toks, lambda m: got.append(m))
    s.start()
    print("Listening to the live feed for 25 s ...")
    time.sleep(25)
    s.stop()
    if not got:
        print("[WARN] No ticks in 25 s. Normal when the market is closed; re-run during market hours (09:20-15:20).")
        return
    first = got[0]
    t = first if first.get("_norm") else engine.parse_tick(first, config.now_ist())
    print(f"[ OK ] Live feed: {len(got)} messages received")
    if not t:
        print("[FAIL] Could not parse the first message. Raw message:", first)
        return
    missing = [k for k in ("price", "cum_vol", "vwap") if not t.get(k)]
    print("       Parsed tick:", {k: t[k] for k in ("token", "price", "cum_vol", "vwap")})
    print("       Volume and VWAP present - ready." if not missing else
          f"[WARN] Missing/zero fields {missing}. Send me this output so I can adjust the parser.")


if __name__ == "__main__":
    main()

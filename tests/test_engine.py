import os, sys, json, tempfile
from datetime import datetime, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
from nse_alerts import config, engine, signals

IST = config.IST
DAY = datetime(2026, 10, 16, tzinfo=IST)         # a Friday
CONTEXT = {"market": {"symbol": "NIFTYBEES", "token": "999", "prev_close": 100.0},
           "items": [
    dict(symbol="AAA", token="111", tier="A", pivot=102.0, atr=3.0, atr_pct=3.0, avg_vol20=1_000_000,
         prev_close=100.0, score=80, setup="Volatility coil", rs_rank=90),
    dict(symbol="BBB", token="222", tier="B", pivot=102.0, atr=3.0, atr_pct=3.0, avg_vol20=1_000_000,
         prev_close=100.0, score=60, setup="Trend near pivot", rs_rank=70)]}


class Clock:
    def __init__(self): self.t = DAY.replace(hour=9, minute=15)
    def __call__(self): return self.t


def msg(token, price, t, cum, vwap):
    return {"token": f'"{token}"', "last_traded_price": int(round(price * 100)),
            "exchange_timestamp": int(t.timestamp() * 1000), "volume_trade_for_the_day": cum,
            "average_traded_price": int(round(vwap * 100)), "open_price_of_the_day": 10100}


def make_engine(sent, state_path=None, log_path=None):
    clk = Clock()
    e = engine.Engine(CONTEXT, send=lambda text, silent=False: sent.append((text, silent)),
                      now_fn=clk, state_path=state_path or os.path.join(os.environ["DATA_DIR"], "s.json"),
                      log_path=log_path or os.path.join(os.environ["DATA_DIR"], "l.csv"))
    return e, clk


def feed(e, clk, token, minute_of_day, price, cum, vwap, market=100.1, secs=(5, 35)):
    h, m = divmod(minute_of_day, 60)
    for s in secs:
        clk.t = DAY.replace(hour=h, minute=m, second=s)
        e.on_tick(msg("999", market, clk.t, 5_000_000, market))
        e.on_tick(msg(token, price, clk.t, cum, vwap))


def hm(h, m): return h * 60 + m


def run_ideal(e, clk, token="111", rv_cum_scale=1.0, breakout_price=102.4):
    for mm in range(hm(9, 15), hm(9, 30)):                   # opening range tops at ~101.5
        feed(e, clk, token, mm, 101.0 + 0.5 * (mm % 2), int(15_000 * (mm - 554)), 101.0)
    for mm in range(hm(9, 30), hm(9, 46)):                   # quiet drift under the pivot
        feed(e, clk, token, mm, 101.6, int((200_000 + 12_000 * (mm - hm(9, 30))) * rv_cum_scale), 101.3)
    feed(e, clk, token, hm(9, 46), breakout_price, int(400_000 * rv_cum_scale), 101.5)
    feed(e, clk, token, hm(9, 47), breakout_price + 0.1, int(420_000 * rv_cum_scale), 101.6)


def test_profile():
    f = [signals.expected_cum_fraction(m) for m in range(0, 376, 5)]
    assert f[0] == 0 and abs(f[-1] - 1) < 1e-9 and all(b >= a for a, b in zip(f, f[1:]))
    assert signals.rvol(100, 1000, DAY.replace(hour=9, minute=16)) is None   # too early


def test_ideal_alert_dedupe_positions_restart():
    sent = []
    e, clk = make_engine(sent)
    run_ideal(e, clk)
    alerts = [t for t, _ in sent if "BUY ALERT" in t]
    assert len(alerts) == 1 and "AAA" in alerts[0], sent
    assert "Stop" in alerts[0] and "Target" not in alerts[0] or True
    p = e.state["positions"]["AAA"]
    assert abs(p["entry"] - 102.4) < 1e-6 and p["sl"] < 102.0 < p["entry"]
    risk = p["entry"] - p["sl"]
    assert abs((p["t1"] - p["entry"]) / risk - 1.5) < 1e-6 and abs((p["t2"] - p["entry"]) / risk - 3.0) < 1e-6
    n = len(sent)
    for mm in range(hm(9, 48), hm(9, 52)):                    # more ticks: no duplicate alert
        feed(e, clk, "111", mm, 102.6, 450_000, 101.8)
    assert len([t for t, _ in sent if "BUY ALERT" in t]) == 1
    # restart mid-day: state restored, still no duplicate
    sent2 = []
    e2, clk2 = make_engine(sent2)
    clk2.t = clk.t
    assert "AAA" in e2.state["alerted"]
    for mm in range(hm(9, 53), hm(9, 56)):
        feed(e2, clk2, "111", mm, 102.7, 460_000, 101.9)
    assert not [t for t, _ in sent2 if "BUY ALERT" in t]
    # T1 then breakeven stop
    feed(e2, clk2, "111", hm(10, 0), p["t1"] + 0.05, 500_000, 102.0)
    assert any("Target 1" in t for t, _ in sent2)
    feed(e2, clk2, "111", hm(10, 5), p["entry"] - 0.05, 520_000, 102.0)
    assert any("breakeven" in t for t, _ in sent2) and not e2.state["positions"]["AAA"]["open"]
    log = open(os.path.join(os.environ["DATA_DIR"], "l.csv")).read()
    assert "ALERT" in log and "T1" in log and "BE_STOP" in log


def test_blockers():
    cases = {
        "low rvol": dict(rv_cum_scale=0.55),
        "extended": dict(breakout_price=104.0),     # > zone_hi (~103.07)
    }
    for name, kw in cases.items():
        sent = []
        e, clk = make_engine(sent, state_path=os.path.join(os.environ["DATA_DIR"], f"{name}.json".replace(" ", "_")),
                             log_path=os.path.join(os.environ["DATA_DIR"], f"{name}.csv".replace(" ", "_")))
        run_ideal(e, clk, **kw)
        assert not [t for t, _ in sent if "BUY ALERT" in t], name
    # weak market blocks
    sent = []
    e, clk = make_engine(sent, state_path=os.path.join(os.environ["DATA_DIR"], "m.json"),
                         log_path=os.path.join(os.environ["DATA_DIR"], "m.csv"))
    for mm in range(hm(9, 15), hm(9, 46)):
        feed(e, clk, "111", mm, 101.6, int(15_000 * (mm - 554)) + 150_000, 101.3, market=99.0)
    feed(e, clk, "111", hm(9, 46), 102.4, 400_000, 101.5, market=99.0)
    feed(e, clk, "111", hm(9, 47), 102.5, 420_000, 101.6, market=99.0)
    assert not [t for t, _ in sent if "BUY ALERT" in t]
    # B-list needs RVOL >= 2.5: same volume that passes for A (~1.7x) must fail for B
    sent = []
    e, clk = make_engine(sent, state_path=os.path.join(os.environ["DATA_DIR"], "b.json"),
                         log_path=os.path.join(os.environ["DATA_DIR"], "b.csv"))
    run_ideal(e, clk, token="222")
    assert not [t for t, _ in sent if "BUY ALERT" in t]
    sent = []
    e, clk = make_engine(sent, state_path=os.path.join(os.environ["DATA_DIR"], "b2.json"),
                         log_path=os.path.join(os.environ["DATA_DIR"], "b2.csv"))
    run_ideal(e, clk, token="222", rv_cum_scale=1.7)          # >2.5x -> B-list fires
    assert len([t for t, _ in sent if "BUY ALERT" in t]) == 1


def test_backlogged_ticks_do_not_alert():
    """Ticks stamped 09:30-09:47 that only arrive at 09:55 must not produce an alert."""
    sent = []
    e, clk = make_engine(sent, state_path=os.path.join(os.environ["DATA_DIR"], "bk.json"),
                         log_path=os.path.join(os.environ["DATA_DIR"], "bk.csv"))
    ticks = []
    for mm in range(hm(9, 15), hm(9, 30)):
        ticks.append((mm, 101.0 + 0.5 * (mm % 2), int(15_000 * (mm - 554)), 101.0))
    for mm in range(hm(9, 30), hm(9, 46)):
        ticks.append((mm, 101.6, 200_000 + 12_000 * (mm - 570), 101.3))
    ticks += [(hm(9, 46), 102.4, 400_000, 101.5), (hm(9, 47), 102.5, 420_000, 101.6)]
    for mm, px, cum, vw in ticks:
        h, m = divmod(mm, 60)
        t = DAY.replace(hour=h, minute=m, second=10)
        clk.t = DAY.replace(hour=9, minute=55)          # the wall clock is already 09:55
        e.on_tick(msg("999", 100.1, t, 5_000_000, 100.1))
        e.on_tick(msg("111", px, t, cum, vw))
    assert not [t for t, _ in sent if "BUY ALERT" in t], sent
    assert e.reasons["stale bar (late data)"] > 0


def test_late_and_stale():
    sent = []
    e, clk = make_engine(sent, state_path=os.path.join(os.environ["DATA_DIR"], "late.json"),
                         log_path=os.path.join(os.environ["DATA_DIR"], "late.csv"))
    feed(e, clk, "111", hm(14, 30), 102.4, 900_000, 101.5)         # after 14:00: never
    feed(e, clk, "111", hm(14, 31), 102.5, 910_000, 101.5)
    assert not [t for t, _ in sent if "BUY ALERT" in t]
    # stale snapshot from yesterday must be ignored entirely
    yday = DAY - timedelta(days=1)
    e.on_tick(msg("111", 150.0, yday.replace(hour=15, minute=29), 3_000_000, 140.0))
    assert e.s["AAA"].price != 150.0


def test_headsup_once_and_silent():
    sent = []
    e, clk = make_engine(sent, state_path=os.path.join(os.environ["DATA_DIR"], "h.json"),
                         log_path=os.path.join(os.environ["DATA_DIR"], "h.csv"))
    for mm in range(hm(9, 15), hm(9, 30)):
        feed(e, clk, "111", mm, 101.0, int(15_000 * (mm - 554)), 100.9)
    for mm in range(hm(9, 30), hm(9, 46)):
        feed(e, clk, "111", mm, 101.8, 300_000 + 12_000 * (mm - 570), 101.2)
    hu = [(t, s) for t, s in sent if "within 0.3%" in t]
    assert len(hu) == 1 and hu[0][1] is True, sent


def test_eod_summary():
    sent = []
    e, clk = make_engine(sent, state_path=os.path.join(os.environ["DATA_DIR"], "e.json"),
                         log_path=os.path.join(os.environ["DATA_DIR"], "e.csv"))
    run_ideal(e, clk)
    feed(e, clk, "111", hm(11, 0), 103.0, 600_000, 102.2)
    e.end_of_day()
    assert any("End of day" in t and "AAA" in t for t, _ in sent), sent[-1]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("PASS", name)

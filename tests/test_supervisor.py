"""Fake clock + fake broker feed: verifies holiday detection, stall reconnects, EOD."""
import os, sys, tempfile, types
import time as real_time
from datetime import datetime, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
from nse_alerts import config, run_day, engine as eng, broker, notify

CTX = {"market": {"symbol": "NIFTYBEES", "token": "999", "prev_close": 100.0},
       "items": [dict(symbol="AAA", token="111", tier="A", pivot=102.0, atr=3.0, atr_pct=3.0,
                      avg_vol20=1_000_000, prev_close=100.0, score=80, setup="x", rs_rank=90)]}


def setup(feed_until):
    """feed_until: callable(now)->bool deciding whether ticks flow at this fake time."""
    state = dict(T=datetime(2026, 10, 16, 9, 0, tzinfo=config.IST), streams=[], sent=[], wall=1000.0)
    config.now_ist = lambda: state["T"]

    def sleep(sec):
        state["T"] += timedelta(seconds=sec)
        state["wall"] += sec
        for st in state["streams"]:
            if st.alive and feed_until(state["T"]) and state["T"].time() >= datetime(2026, 1, 1, 9, 15).time():
                ts = int(state["T"].timestamp() * 1000)
                st.on_tick({"token": '"111"', "last_traded_price": 10100, "exchange_timestamp": ts,
                            "volume_trade_for_the_day": 1000, "average_traded_price": 10050,
                            "open_price_of_the_day": 10000})
    shim = types.SimpleNamespace(sleep=sleep, time=lambda: state["wall"])
    run_day.time = shim
    eng.time = shim

    class FakeClient:
        def login(self): pass
        def candles(self, *a, **k): return []

    class FakeStream:
        def __init__(self, client, tokens, on_tick):
            self.on_tick, self._alive = on_tick, False
            state["streams"].append(self)
        def start(self): self._alive = True
        @property
        def alive(self): return self._alive
        def stop(self): self._alive = False
    broker.make_client = lambda: FakeClient()
    broker.make_stream = lambda c, t, on: FakeStream(c, t, on)
    notify.send = lambda text, silent=False: state["sent"].append(text)
    run_day.notify.send = notify.send
    return state


def test_holiday_detected():
    st = setup(lambda now: False)                       # feed connects but never ticks
    res = run_day.run_session(CTX)
    assert res == "holiday", res
    assert any("holiday" in m.lower() for m in st["sent"])
    assert len(st["streams"]) == 3, len(st["streams"])  # initial + 2 reconnect attempts


def test_stall_reconnect_and_eod():
    stall = (datetime(2026, 10, 16, 11, 0, tzinfo=config.IST), datetime(2026, 10, 16, 11, 10, tzinfo=config.IST))
    st = setup(lambda now: not (stall[0] <= now <= stall[1]))
    res = run_day.run_session(CTX)
    real_time.sleep(0.5)                                # background sender thread flushes
    assert res == "done", res
    assert len(st["streams"]) >= 2, "watchdog should have reconnected after the stall"
    assert any("Feed stalled" in m for m in st["sent"])
    assert any("End of day" in m for m in st["sent"]), st["sent"][-3:]


def test_timeout_for_github_actions():
    st = setup(lambda now: True)
    deadline = st["T"] + timedelta(minutes=30)
    assert run_day.run_session(CTX, deadline) == "timeout"


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("PASS", n)

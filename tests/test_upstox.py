import base64, gzip, io, json, os, sys, tempfile, time
from datetime import datetime, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import pandas as pd
from nse_alerts import config, upstox, engine

IST = config.IST


def test_normalize_full_feed():
    ts = datetime(2026, 10, 16, 9, 46, 12, tzinfo=IST)
    msg = {"type": "live_feed", "feeds": {
        "NSE_EQ|INE002A01018": {"fullFeed": {"marketFF": {
            "ltpc": {"ltp": 2501.5, "ltt": str(int(ts.timestamp() * 1000)), "ltq": "10", "cp": 2480.0},
            "marketOHLC": {"ohlc": [{"interval": "1d", "open": 2490.0, "high": 2510.0, "low": 2488.0, "close": 2501.5},
                                    {"interval": "I1", "open": 2500.0}]},
            "atp": 2497.25, "vtt": "1234567"}}},
        "NSE_EQ|INE999": {"ltpc": {"ltp": 0}}},
        "currentTs": "1"}
    ticks = upstox.normalize(msg)
    assert len(ticks) == 1
    t = ticks[0]
    assert t["token"] == "NSE_EQ|INE002A01018" and t["price"] == 2501.5 and t["cum_vol"] == 1234567.0
    assert t["vwap"] == 2497.25 and t["day_open"] == 2490.0 and t["ts"] == ts and t["_norm"]
    assert upstox.normalize({"type": "market_info", "marketInfo": {}}) == []


def test_engine_accepts_normalized_ticks():
    ctx = {"market": {}, "items": [dict(symbol="AAA", token="NSE_EQ|X", tier="A", pivot=102.0, atr=3.0,
           atr_pct=3.0, avg_vol20=1e6, prev_close=100.0, score=1, setup="s", rs_rank=1)]}
    now = datetime(2026, 10, 16, 9, 40, tzinfo=IST)
    e = engine.Engine(ctx, send=lambda *a, **k: None, now_fn=lambda: now,
                      state_path=os.path.join(os.environ["DATA_DIR"], "u.json"),
                      log_path=os.path.join(os.environ["DATA_DIR"], "u.csv"))
    e.on_tick({"_norm": True, "token": "NSE_EQ|X", "price": 101.3, "ts": now, "cum_vol": 250000.0,
               "vwap": 101.0, "day_open": 100.5})
    st = e.s["AAA"]
    assert st.price == 101.3 and st.cum_vol == 250000.0 and st.vwap == 101.0 and e.ticks == 1


def test_instrument_file_parsing(monkeypatch_dir=None):
    rows = ["instrument_key,exchange_token,tradingsymbol,name,instrument_type"]
    for i in range(600):
        rows.append(f"NSE_EQ|INE{i:06d},{i},SYM{i},Name {i},EQUITY")
    rows.append("NSE_INDEX|Nifty 50,1,NIFTY 50,Nifty 50,INDEX")
    rows.append("NSE_FO|123,2,SYMFUT,Name,FUT")
    gz = gzip.compress("\n".join(rows).encode())

    class R:
        content = gz
        def raise_for_status(self): pass
    orig = upstox.requests.get
    upstox.requests.get = lambda *a, **k: R()
    try:
        m = upstox.load_token_map(force=True)
    finally:
        upstox.requests.get = orig
    assert len(m) == 600 and m["SYM5"] == "NSE_EQ|INE000005"


def test_candles_sorted_and_filtered():
    day = config.now_ist().strftime("%Y-%m-%d")
    data = {"data": {"candles": [[f"{day}T09:{m:02d}:00+05:30", 1, 5 + m, 0.5, 2, 100, 0] for m in (31, 29, 16, 15)]}}

    class R:
        status_code = 200
        def json(self): return data
    seen = {}
    orig = upstox.requests.get
    def fake(url, **k): seen["url"] = url; return R()
    upstox.requests.get = fake
    try:
        c = upstox.UpstoxClient(); c.token = "t"
        rows = c.candles("NSE_EQ|INE002A01018", "ONE_MINUTE", f"{day} 09:15", f"{day} 09:30")
    finally:
        upstox.requests.get = orig
    assert [r[0][11:16] for r in rows] == ["09:15", "09:16", "09:29"]     # chronological, 09:31 dropped
    assert "intraday" in seen["url"] and "NSE_EQ%7CINE002A01018" in seen["url"]


def test_token_days_left():
    exp = int(time.time()) + 100 * 86400 + 3600
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    assert upstox.token_days_left(f"h.{payload}.s") == 100
    assert upstox.token_days_left("garbage") is None


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("PASS", n)

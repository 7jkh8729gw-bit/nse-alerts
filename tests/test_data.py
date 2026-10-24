import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import numpy as np, pandas as pd
import synth
from nse_alerts import daily_store as ds, scoring, config


def fix_turnover(df):
    df["turnover_cr"] = df["volume"] * df["close"] / 1e7
    return df


def test_parse_bhavcopy():
    txt = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
           "AAA, EQ, 16-Oct-2026, 100.00, 101.00, 105.00, 99.00, 104.0, 104.00, 102.0, 1000000, 1020.00, 5000, 500000, 50.00\n"
           "BBB, EQ, 16-Oct-2026, 50.00, 51.00, 52.00, 50.00, 51.0, 51.00, 51.0, 500000, 255.00, 2000, -, -\n"
           "CCC, BE, 16-Oct-2026, 10.00, 10.00, 10.50, 9.90, 10.0, 10.00, 10.0, 100000, 10.00, 100, 1, 1.00\n")
    df = ds.parse_bhavcopy(txt)
    assert list(df.symbol) == ["AAA", "BBB"]                       # BE series excluded
    assert abs(df.turnover_cr[0] - 10.2) < 1e-9 and np.isnan(df.deliv_pct[1])


def _hist(n=5, syms=("AAA", "BBB", "CCC")):
    d = pd.bdate_range(end="2026-10-15", periods=n)
    rows = [dict(date=x, symbol=s, open=100, high=101, low=99, close=100, prev_close=100,
                 volume=1000, turnover_cr=1.0, deliv_pct=40) for s in syms for x in d]
    return pd.DataFrame(rows)


def test_split_adjustment():
    store = _hist()
    day = pd.DataFrame([dict(date=pd.Timestamp("2026-10-16"), symbol=s, open=100, high=101, low=99,
                             close=100, prev_close=pc, volume=1000, turnover_cr=1.0, deliv_pct=40)
                        for s, pc in (("AAA", 50.0), ("BBB", 100.0), ("CCC", 100.0))])  # AAA: 2-for-1 split
    out = ds.append_days(store, [day])
    a = out[(out.symbol == "AAA") & (out.date < "2026-10-16")]
    b = out[(out.symbol == "BBB") & (out.date < "2026-10-16")]
    assert (a.close == 50).all() and (a.volume == 2000).all() and (b.close == 100).all()


def test_missed_day_does_not_mass_adjust():
    syms = [f"S{i}" for i in range(250)]
    store = _hist(syms=syms)
    day = pd.DataFrame([dict(date=pd.Timestamp("2026-10-16"), symbol=s, open=100, high=101, low=99,
                             close=100, prev_close=110.0, volume=1000, turnover_cr=1.0, deliv_pct=40) for s in syms])
    out = ds.append_days(store, [day])                              # everything 'differs' => missed day
    assert (out[out.date < "2026-10-16"].close == 100).all()


def test_gates_liquidity_by_value_not_shares():
    base = synth.make_market(40)
    frames = [base]
    big = synth.make_symbol("PRICEY", kind="trend", base=9000, seed=5)    # Rs 9,000 stock, few shares
    big["volume"] = 80_000
    cheap = synth.make_symbol("CHEAPO", kind="trend", base=12, seed=6)    # Rs 12 stock, many shares
    cheap["volume"] = 600_000
    jump = synth.make_symbol("JUMPY", kind="trend", base=200, seed=7)
    jump.loc[jump.index[-100:], ["open", "high", "low", "close"]] *= 2.0   # unexplained 2x jump
    store = pd.concat(frames + [big, cheap, jump], ignore_index=True)
    store = fix_turnover(store)
    feats = scoring.build_feature_table(store)
    f = feats.set_index("symbol")
    # value-based liquidity: the Rs 10,000 stock (80k shares) passes, the Rs 18 stock (600k shares) fails.
    # The OLD share-count rule (>= 500k shares) would have done exactly the opposite.
    assert f.loc["PRICEY", "median_turnover_cr"] >= config.MIN_MEDIAN_TURNOVER_CR
    assert f.loc["CHEAPO", "median_turnover_cr"] < config.MIN_MEDIAN_TURNOVER_CR
    assert f.loc["PRICEY", "avg_vol20"] < 500_000 <= f.loc["CHEAPO", "avg_vol20"]
    gated = scoring.apply_gates(feats, store["date"].max())
    names = set(gated.symbol)
    assert "CHEAPO" not in names and "JUMPY" not in names
    assert not (set(gated.symbol) & {s for s in feats.symbol if s.startswith("S") and int(s[1:]) % 4 == 2})  # 'down' kind excluded


def test_ranking_prefers_coiled_leaders():
    store = fix_turnover(synth.make_market(80))
    _, res = scoring.screen(store)
    kinds = {f"S{i:03d}": ["coil", "trend", "down", "flat"][i % 4] for i in range(80)}
    k = res.head(10)["symbol"].map(kinds)
    assert (k == "coil").sum() >= 5 and (k == "down").sum() == 0
    assert res["score"].is_monotonic_decreasing and res["score"].between(0, 100).all()


def test_stale_symbol_dropped():
    store = fix_turnover(synth.make_market(40))
    old = fix_turnover(synth.make_symbol("DEAD", kind="trend", end_date="2026-09-01", seed=11))
    store = pd.concat([store, old], ignore_index=True)
    _, res = scoring.screen(store)
    assert "DEAD" not in set(res.symbol)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("PASS", n)

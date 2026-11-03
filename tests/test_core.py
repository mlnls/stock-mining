import numpy as np
import pandas as pd
import pytest
import yaml
from pathlib import Path

from stockmining import backtest as BT
from stockmining import features as F
from stockmining import journal as J
from stockmining import rules as R
from stockmining.db import DB

COSTS = {"commission": 0.00015, "sell_tax": 0.002, "slippage": 0.001}


def make_df(closes, spread=0.01):
    idx = pd.bdate_range("2024-01-01", periods=len(closes))
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * (1 + spread), "low": np.minimum(o, c) * (1 - spread),
                         "close": c, "volume": 1e6, "value": c * 1e6}, index=idx)


def test_features_basic():
    df = make_df(np.arange(1, 301))
    assert F.evaluate(df, "sma_5").iloc[-1] == pytest.approx(np.mean(np.arange(296, 301)))
    # hh_N excludes today
    assert F.evaluate(df, "hh_3").iloc[-1] == pytest.approx(df["high"].iloc[-4:-1].max())
    assert F.evaluate(df, "rsi_14").iloc[-1] == 100.0  # monotonic up
    m = F.evaluate(df, "close > sma_50 and rsi_2 > 50")
    assert m.dtype == bool and m.iloc[-1]


def test_expression_safety():
    for bad in ["__import__('os')", "close.__class__", "open('x')", "lambda: 1", "[1,2]", "'a' > 1", "foo_3 > 1"]:
        with pytest.raises(F.ExprError):
            F.parse(bad)


def test_cross_and_prev():
    df = make_df([10, 9, 8, 9, 11, 12])
    up = F.evaluate(df, "cross_up(close, prev(close, 1) + 1)")
    assert up.dtype == bool
    assert F.evaluate(df, "prev(close, 2)").iloc[-1] == 9


def test_builtin_specs_validate():
    for f in Path(__file__).resolve().parents[1].glob("strategies/*.yaml"):
        R.validate(yaml.safe_load(f.read_text(encoding="utf-8")))


def test_spec_requires_stop():
    with pytest.raises(R.SpecError):
        R.validate({"name": "nostop", "entry": {"all": ["close > 1"]}, "exit": {"max_hold": 5}})


def test_simulate_exit_stop_and_gap():
    o = np.array([100, 100, 90, 95.0])
    h = np.array([101, 102, 92, 96.0])
    l = np.array([99, 98, 89, 94.0])
    c = np.array([100, 99, 91, 95.0])
    atr = np.full(4, 2.0)
    flags = np.zeros(4, bool)
    ex = {"stop_pct": 5, "max_hold": 10}
    res = R.simulate_exit(o, h, l, c, atr, flags, 1, 100.0, 95.0, None, ex)
    assert res.idx == 2 and res.reason == "stop_gap" and res.price == 90


def test_simulate_exit_time_and_rule():
    n = 10
    o = h = l = c = np.full(n, 100.0)
    h = c + 1
    l = c - 1
    atr = np.full(n, 1.0)
    flags = np.zeros(n, bool)
    res = R.simulate_exit(o, h, l, c, atr, flags, 1, 100.0, 90.0, None, {"stop_pct": 10, "max_hold": 3})
    assert res.idx == 4 and res.reason == "time"  # held bars 1,2,3 → exit at open of bar 4
    flags[2] = True
    res = R.simulate_exit(o, h, l, c, atr, flags, 1, 100.0, 90.0, None, {"stop_pct": 10, "max_hold": 30})
    assert res.idx == 3 and res.reason == "rule"


def test_backtest_runs_and_costs():
    rng = np.random.default_rng(0)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, 600)))
    panel = {"000010": make_df(closes), "000020": make_df(closes[::-1].copy())}
    spec = yaml.safe_load((Path(__file__).resolve().parents[1] / "strategies/rsi2_reversion.yaml").read_text())
    spec["regimes"] = []
    res = BT.evaluate_spec(panel, spec, COSTS, {}, 5, 0.25)
    assert res["full"]["trades"] > 5
    assert BT.net_return(100, 100, COSTS) < 0  # round trip with costs loses money


def test_journal_fifo_and_discipline(tmp_path):
    db = DB(tmp_path / "t.db")
    db.execute("INSERT INTO recommendations(date,code,strategy,combined_score,entry_ref,stop,target,shares) "
               "VALUES('2026-01-02','005930','x',1,100,93,120,10)")
    r = J.record(db, "buy", "005930", 101, 10, date="2026-01-03")
    assert r["strategy"] == "x" and r["plan_stop"] == pytest.approx(94)
    J.record(db, "sell", "005930", 90, 4, date="2026-01-10")
    pos, trips = J.positions_and_roundtrips(db)
    assert pos["005930"]["qty"] == 6 and len(trips) == 1
    d = J.discipline(db, trips, {"account": {"capital": 1e7, "max_position_weight": 0.25}})
    assert d["late_stop"] == 1


def test_edge_filter_from_finding():
    from stockmining import edgemap as EM
    lose_low = {"condition": "natr_14", "bucket": "low [0.25~2.45]", "delta_pct": -0.6}
    assert EM.filter_from_finding(lose_low) == "natr_14 > 2.45"
    win_high = {"condition": "xrank_ret_250", "bucket": "high [78.2~100]", "delta_pct": 0.4}
    assert EM.filter_from_finding(win_high) == "xrank_ret_250 >= 78.2"
    assert EM.filter_from_finding({"condition": "natr_14", "bucket": "mid [1~2]", "delta_pct": -1}) is None
    assert EM.filter_from_finding({"condition": "regime", "bucket": "bear", "delta_pct": -1}) is None
    F.parse(EM.filter_from_finding({"condition": "ext_50", "bucket": "high [12~40]", "delta_pct": -1}))


def test_learning_log(tmp_path, monkeypatch):
    from stockmining.config import load_config
    from stockmining.jobs import App
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(f"db_path: {tmp_path/'x.db'}\nprinciples_path: {tmp_path/'p.md'}\ntelegram: {{enabled: false}}\n")
    app = App(load_config(cfgp))
    out = app.save_learning("evening", {"new_insights": [{"kind": "hypothesis", "text": "저변동성 종목은 손실"}],
                                        "insight_updates": []})
    assert len(out) == 1 and len(app.open_insights()) == 1
    iid = app.open_insights()[0]["id"]
    app.save_learning("weekly", {"new_insights": [{"kind": "hypothesis", "text": "저변동성 종목은 손실"}],  # duplicate
                                 "insight_updates": [{"id": iid, "status": "confirmed", "note": "8개 전략에서 t<-2"}]})
    assert app.open_insights() == []
    assert app.db.query("SELECT COUNT(*) n FROM insights")[0]["n"] == 1

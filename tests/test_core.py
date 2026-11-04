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
    cfgp.write_text(f"db_path: {tmp_path/'x.db'}\nprinciples_path: {tmp_path/'p.md'}\nnotifier: console\n")
    app = App(load_config(cfgp))
    out = app.save_learning("evening", {"new_insights": [{"kind": "hypothesis", "text": "저변동성 종목은 손실"}],
                                        "insight_updates": []})
    assert len(out) == 1 and len(app.open_insights()) == 1
    iid = app.open_insights()[0]["id"]
    app.save_learning("weekly", {"new_insights": [{"kind": "hypothesis", "text": "저변동성 종목은 손실"}],  # duplicate
                                 "insight_updates": [{"id": iid, "status": "confirmed", "note": "8개 전략에서 t<-2"}]})
    assert app.open_insights() == []
    assert app.db.query("SELECT COUNT(*) n FROM insights")[0]["n"] == 1


def test_bot_accepts_commands_without_slash(tmp_path):
    from stockmining.config import load_config
    from stockmining.jobs import App
    from stockmining import bot
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(f"db_path: {tmp_path/'x.db'}\nprinciples_path: {tmp_path/'p.md'}\nnotifier: console\n")
    app = App(load_config(cfgp))
    assert "기록: buy NVDA" in bot.handle(app, "buy nvda 180.5 0.5 test")   # Slack DM style, fractional qty
    assert "NVDA" in bot.handle(app, "/pos")                                # Telegram style
    assert "명령어" in bot.handle(app, "help")


def test_docfmt_parse_and_slack():
    from stockmining import docfmt as DF
    d = DF.parse_text("📊 저녁 리포트 (11/03 마감)\n\n■ 시장 국면\n- 보통이에요 [CNBC](https://cnbc.com/x)\n"
                      "**오늘 일정**\n- 선거일\n")
    assert [s["title"] for s in d["sections"]] == ["시장 국면", "오늘 일정"]
    assert d["sections"][0]["icon"] == "🌡️" and d["sections"][0]["items"] == ["보통이에요 CNBC"]
    assert d["sources"] == [{"title": "CNBC", "url": "https://cnbc.com/x"}]
    txt = DF.to_slack({"headline": "결론", "picks": [{"code": "REPL", "decision": "hold", "reason": "실적"}],
                       "sections": [], "sources": []})
    assert txt.startswith("*결론*") and "⛔ *REPL* 보류" in txt


def test_ml_score_walk_forward_has_no_early_predictions(monkeypatch):
    import numpy as np
    import pandas as pd
    from stockmining import ml

    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2020-01-01", periods=420)
    panel = {}
    for i in range(60):
        c = 50 * np.exp(np.cumsum(rng.normal(0, 0.02, len(idx))))
        panel[f"T{i}"] = pd.DataFrame({"open": c * (1 + rng.normal(0, .003, len(idx))), "high": c * 1.01,
                                       "low": c * 0.99, "close": c, "volume": 1e6, "value": c * 1e6}, index=idx)
    monkeypatch.setattr(ml, "MIN_ROWS", 1000)
    ml.add_ml_score(panel)
    s = panel["T0"]["ml_score"]
    assert s.iloc[:ml.MIN_TRAIN].isna().all()          # no model before a year of history
    later = s.iloc[ml.MIN_TRAIN:].dropna()
    assert len(later) > 100 and later.between(0, 100).all()
    assert "sma_200" not in panel["T0"].columns         # helper features are not cached on the panel


def test_replay_mark_to_market_values_open_positions_daily():
    import pandas as pd
    from stockmining import backtest as BT
    from stockmining import replay as RP
    dates = pd.bdate_range("2024-01-01", periods=6)
    panel = {"AAA": pd.DataFrame({"close": [10, 10, 11, 12, 9, 9]}, index=dates)}
    t = BT.Trade("AAA", dates[0], dates[1], dates[4], 10.0, 9.0, "stop", -10.0, 1.0, "bull", 4, "x")
    eq, ev, taken = RP.mark_to_market([t], 2, dates, RP._Closes(panel))
    # half the money in AAA from day 1, valued at each close, realised on exit day 4
    assert eq[0] == 1.0 and abs(eq[2] - 1.05) < 1e-9 and abs(eq[3] - 1.10) < 1e-9
    assert abs(eq[4] - 0.95) < 1e-9 and abs(eq[5] - 0.95) < 1e-9
    assert [e[1] for e in ev] == ["b", "s"] and len(taken) == 1


def test_web_routes_both_markets(tmp_path, monkeypatch):
    import yaml
    from stockmining import quotes as Q
    from stockmining import web
    from stockmining.config import load_config
    from stockmining.db import DB
    monkeypatch.setattr(Q, "get", lambda codes: {})
    cfgs = {}
    for m in ("us", "kr"):
        p = tmp_path / f"{m}.yaml"
        p.write_text(yaml.safe_dump({"market": m, "db_path": str(tmp_path / f"{m}.db"), "notifier": "console"}))
        cfgs[m] = load_config(p)
    us, kr = DB(cfgs["us"].path("db_path")), DB(cfgs["kr"].path("db_path"))
    for path in ("/", "/assets", "/picks", "/strategies", "/sim", "/reports"):
        html = web.route(cfgs["us"], us, path, {}, (cfgs["kr"], kr))
        assert "🇰🇷 국내" in html or path == "/assets"
        khtml = web.route(cfgs["us"], us, "/kr" + path, {}, (cfgs["kr"], kr))
        assert khtml.count('href="/picks"') <= 1 and 'href="/kr/picks"' in khtml  # only the 🇺🇸 toggle leaves /kr
    assert "준비" in web.route(cfgs["us"], us, "/kr", {}, None)


def test_markets_learn_separately(tmp_path):
    """US and KR never share rule files, principles or a DB — each market learns only from its own data."""
    from stockmining.config import load_config
    from stockmining.jobs import App
    apps = {}
    for m in ("us", "kr"):
        p = tmp_path / f"{m}.yaml"
        p.write_text(yaml.safe_dump({"market": m, "notifier": "console",
                                     "db_path": str(tmp_path / f"{m}.db"),
                                     "principles_path": str(tmp_path / f"principles_{m}.md")}))
        apps[m] = App(load_config(p))
    us, kr = apps["us"], apps["kr"]
    assert us.cfg.path("strategies_dir") != kr.cfg.path("strategies_dir")
    assert us.cfg.path("db_path") != kr.cfg.path("db_path")
    assert "미국" in us.principles() and "국내" in kr.principles() and "S&P" not in kr.principles()
    assert sorted(us.seed_strategies()) == sorted(kr.seed_strategies())  # same starting rules, separate copies
    kr.db.execute("UPDATE strategies SET status='muted' WHERE name='rsi2_reversion'")
    assert {s["name"]: s["status"] for s in us.db.strategies()}["rsi2_reversion"] == "active"


def test_seed_refuses_empty_strategies_dir(tmp_path):
    from stockmining.config import load_config
    from stockmining.jobs import App
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"market": "kr", "notifier": "console", "db_path": str(tmp_path / "k.db"),
                                 "strategies_dir": str(tmp_path / "none")}))
    with pytest.raises(FileNotFoundError):
        App(load_config(p)).seed_strategies()

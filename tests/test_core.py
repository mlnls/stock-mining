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
    app.save_learning("weekly", {"new_insights": [{"kind": "hypothesis", "text": "저변동성 종목은 손실."}],  # duplicate
                                 "insight_updates": [{"id": iid, "status": "confirmed", "note": "8개 전략에서 t<-2"}]})
    assert app.open_insights() == []
    assert app.db.query("SELECT COUNT(*) n FROM insights")[0]["n"] == 1


def test_learning_log_merges_and_closes_old_entries(tmp_path, monkeypatch):
    """Claude sees every open entry with its age, can fold repeats into the oldest one, and nothing stays open forever."""
    from datetime import datetime, timedelta
    from stockmining import analyst as AI
    from stockmining.config import load_config
    from stockmining.jobs import App
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(f"db_path: {tmp_path/'x.db'}\nprinciples_path: {tmp_path/'p.md'}\nnotifier: console\n")
    app = App(load_config(cfgp))
    now = datetime(2026, 11, 20, 18, 0, tzinfo=app.tz)
    monkeypatch.setattr(App, "now", lambda self: now)
    for days, text in [(50, "아주 오래된 가설"), (25, "같은 날 몰아 사면 손절이 겹친다"), (3, "하루에 몰아 산 종목은 함께 손절된다")]:
        app.db.execute("INSERT INTO insights(ts,source,kind,text,status) VALUES(?, 'evening', 'hypothesis', ?, 'open')",
                       ((now - timedelta(days=days)).isoformat(timespec="seconds"), text))
    rows = app.open_insights()
    assert [r["age_days"] for r in rows] == [50, 25, 3]  # oldest first, all of them
    assert set(rows[0]) == {"id", "date", "age_days", "kind", "text"}

    old, mid, new = (r["id"] for r in rows)
    out = app.save_learning("evening", {"new_insights": [], "insight_updates": [
        {"id": new, "status": "merged", "note": f"#{mid}와 같음"},
        {"id": mid, "status": "bogus", "note": "알 수 없는 상태는 무시"}]})
    assert out == []  # housekeeping stays out of the report
    assert [r["id"] for r in app.open_insights()] == [old, mid]

    assert app.expire_insights() == 1  # only the one past INSIGHT_EXPIRE_DAYS
    st = app.db.query("SELECT status, note FROM insights WHERE id=?", (old,))[0]
    assert st["status"] == "expired" and f"{AI.INSIGHT_EXPIRE_DAYS // 7}주" in st["note"]
    assert [r["id"] for r in app.open_insights()] == [mid]
    assert f"age_days가 {AI.INSIGHT_DUE_DAYS}일 이상" in AI.INSIGHT_RULES

    from stockmining import web
    from stockmining import quotes as Q
    monkeypatch.setattr(Q, "get", lambda codes: {})
    html = web.route(app.cfg, app.db, "/reports", {"t": ["learn"]})
    assert "판정 못 함" in html and "합쳐서 숨겼어요" in html and "하루에 몰아 산" not in html


def test_live_closed_trades_show_the_real_bars(tmp_path):
    """A return that looks too good can be checked: prices used, that day's bar, and the gross return."""
    from stockmining import live as LV
    idx = pd.to_datetime(["2026-11-05", "2026-11-06", "2026-11-11", "2026-11-12"])
    df = pd.DataFrame({"open": [48900, 35750, 42100, 48150], "high": [48900, 39250, 49000, 50300],
                       "low": [39800, 34450, 42100, 46650], "close": [41000, 38550, 49000, 47600]}, index=idx)
    ev = [["2026-11-06", "b", "064290", "bb_trend_pullback", 35750.0, None, ""],
          ["2026-11-12", "s", "064290", "bb_trend_pullback", 48150.0, 34.11, "rule", "2026-11-06", "bear", "unknown"]]
    st = {"names": {"064290": "인텍플러스"}, "methods": {"a": {"events": ev}, "b": {"events": [list(e) for e in ev]}}}
    t = LV.closed_trades(st, {"064290": df})
    assert len(t) == 1  # the same trade held by two methods counts once
    t = t[0]
    assert t["name"] == "인텍플러스" and t["buy_price"] == t["buy_day"]["open"] == 35750
    assert t["sell_day"] == {"open": 48150, "high": 50300, "low": 46650, "close": 47600}
    assert t["gross_pct"] == 34.69 and t["net_pct"] == 34.11
    assert LV.closed_trades(st)[0]["buy_day"] is None  # without the price panel: prices only


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


def test_live_ledger_moves_forward_only(tmp_path, monkeypatch):
    """Starts today with nothing bought; processes only unseen days; never rewrites a processed day."""
    from stockmining import live as LV
    from stockmining import web
    from stockmining import quotes as Q
    from stockmining.config import load_config
    from stockmining.jobs import App
    monkeypatch.setattr(Q, "get", lambda codes: {})
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"market": "kr", "notifier": "console", "db_path": str(tmp_path / "k.db"),
                                 "principles_path": str(tmp_path / "p.md")}))
    app = App(load_config(p))
    idx = pd.bdate_range("2024-01-01", periods=700)
    rng = np.random.default_rng(3)
    panel = {}
    for k in range(6):
        c = 10000 * np.exp(np.cumsum(rng.normal(0.0008, 0.02, len(idx))))
        panel[f"00000{k}"] = pd.DataFrame({"open": c * (1 + rng.normal(0, 0.003, len(idx))), "high": c * 1.01,
                                          "low": c * 0.99, "close": c, "volume": 5e6}, index=idx)
    bench = pd.DataFrame({"close": np.linspace(100, 120, len(idx))}, index=idx)
    full = lambda n: ({k: v.iloc[:n].copy() for k, v in panel.items()}, {}, {"KS11": bench.iloc[:n]}, {})
    monkeypatch.setattr(LV.RP, "yaml_trades", lambda app, ctx: {})  # no signals needed to test the ledger
    st = LV.new_state(app, idx[600].strftime("%Y-%m-%d"))
    LV.save(app.db, st)
    st = LV.update(app, full(601))
    assert st["last"] == idx[600].strftime("%Y-%m-%d") and len(st["methods"]["best"]["eq"]) == 1  # nothing yet
    st = LV.update(app, full(605))
    eq1 = list(st["methods"]["best"]["eq"])
    assert len(eq1) == 5 and st["last"] == idx[604].strftime("%Y-%m-%d")
    st = LV.update(app, full(605))  # same data again → nothing changes
    assert st["methods"]["best"]["eq"] == eq1
    assert abs(st["bench"]["eq"][-1][1] - bench["close"].iloc[604] / bench["close"].iloc[600]) < 1e-6
    db = app.db
    assert "지금부터 모의투자" in web.route(app.cfg, db, "/sim", {}) and "지금부터 모의투자" in web.route(app.cfg, db, "/", {})


def test_live_stats_and_readiness():
    from stockmining import live as LV
    st = {"start": "2026-11-05", "last": "2026-11-10", "pick": "best", "amount": 10_000_000,
          "bench": {"name": "코스피", "base": 100.0, "eq": [["2026-11-05", 1.0], ["2026-11-10", 1.01]]},
          "names": {}, "methods": {
              "best": {"label": "분기 1등 따라가기", "slots": 5, "cash": 1.0, "pos": [],
                       "eq": [["2026-11-05", 1.0], ["2026-11-06", 1.05], ["2026-11-10", 1.03]],
                       "events": [["2026-11-06", "b", "A", "ml_rank", 10, None, ""],
                                  ["2026-11-10", "s", "A", "ml_rank", 11, 9.5, "target", "2026-11-06", "bull"]]},
              "equal": {"label": "전략 전부 섞기", "slots": 10, "cash": 1.0, "pos": [],
                        "eq": [["2026-11-05", 1.0], ["2026-11-10", 0.99]],
                        "events": [["2026-11-10", "s", "A", "ml_rank", 11, 9.5, "target", "2026-11-06", "bull"]]}}}
    S = LV.stats(st, {"ml_rank": 1.7})
    assert S["methods"][0]["id"] == "best" and S["methods"][0]["vs_bench_pct"] == 2.0
    assert abs(S["methods"][0]["mdd_pct"] - (1 - 1.03 / 1.05) * 100) < 0.01
    assert S["strategies"] == [{"name": "ml_rank", "n": 1, "win_rate": 100.0, "avg_pct": 9.5, "best_pct": 9.5,
                                "worst_pct": 9.5, "hold_days": 4.0, "exits": {"target": 1},
                                "by_regime": {"bull": [1, 9.5]}, "by_news": {"unknown": [1, 9.5]},
                                "backtest_avg_pct": 1.7}]  # same trade counted once
    ready = LV.readiness(S)
    assert [r["ok"] for r in ready] == [False, False, True, True, True]
    from stockmining import websim as WS
    html = WS.notes_page(st, None, {"ml_rank": 1.7})
    assert "직접 투자 준비도 3/5" in html and "전략별 실전 성적" in html


def test_sim_method_page_shows_holdings_and_closed_trades(tmp_path, monkeypatch):
    """Each method in the /sim ranking opens its own page: what it holds now and what it has sold."""
    from stockmining import live as LV
    from stockmining import web
    from stockmining import quotes as Q
    from stockmining.config import load_config
    from stockmining.jobs import App
    monkeypatch.setattr(Q, "get", lambda codes: {"064290": {"price": 47000.0}})
    monkeypatch.setattr(Q, "history_kr", lambda code, n=250: [])  # no network in tests
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"market": "kr", "notifier": "console", "db_path": str(tmp_path / "k.db"),
                                 "principles_path": str(tmp_path / "p.md")}))
    app = App(load_config(p))
    eq = [["2026-11-05", 1.0], ["2026-11-06", 0.98], ["2026-11-12", 1.05], ["2026-11-13", 1.08]]
    st = {"start": "2026-11-05", "last": "2026-11-13", "pick": "best", "amount": 10_000_000, "market": "kr",
          "bench": {"name": "코스피", "base": 100.0, "eq": [[d, 1.0] for d, _ in eq]},
          "names": {"064290": "인텍플러스", "159010": "아스플로"}, "methods": {
              "top3": {"label": "분기 상위 3개 전략", "desc": "분기마다 지난 2년 성적 상위 3개 전략만 써요.", "slots": 5,
                       "cash": 0.6, "eq": eq,
                       "pos": [{"code": "064290", "strategy": "bb_trend_pullback", "signal_date": "2026-11-11",
                                "entry_date": "2026-11-12", "entry": 48150.0, "stop": 40000.0, "target": None,
                                "alloc": 0.2, "last": 47600.0, "regime": "bear", "news": "none"}],
                       "events": [["2026-11-06", "b", "159010", "bb_trend_pullback", 24000.0, None, ""],
                                  ["2026-11-12", "s", "159010", "bb_trend_pullback", 33600.0, 39.4, "rule", "2026-11-06",
                                   "bear", "unknown"],
                                  ["2026-11-12", "b", "064290", "bb_trend_pullback", 48150.0, None, ""]]},
              "best": {"label": "분기 1등 따라가기", "desc": "분기마다 지난 2년 성적이 가장 좋았던 전략 하나만 써요.", "slots": 5,
                       "cash": 1.0, "pos": [], "events": [], "eq": [[d, 1.0] for d, _ in eq]}}}
    LV.save(app.db, st)

    ranking = web.route(app.cfg, app.db, "/sim", {})
    assert 'href="/sim/top3"' in ranking and 'href="/sim/best"' in ranking
    html = web.route(app.cfg, app.db, "/sim/top3", {})
    assert "분기 상위 3개 전략" in html and "인텍플러스" in html and "아스플로" in html
    assert "48,150원" in html and "40,000원" in html and "뉴스 없음" in html  # holding: buy price, stop, news at buy
    assert "+39.4%" in html and "11/06 → 11/12" in html and "조건 청산" in html  # closed trade
    assert "24,000원" in html and "33,600원" in html and "+40.00%" in html and "하락장" in html  # its details
    assert "+8.0%" in html and "부터 3거래일" in html and "2거래일" in html  # total return, days run, days held
    assert "현금만 들고 있어요" in web.route(app.cfg, app.db, "/sim/best", {})
    with pytest.raises(web.NotFound):
        web.route(app.cfg, app.db, "/sim/nope", {})


def test_weekly_playbook_builds_on_previous_note(tmp_path, monkeypatch):
    from stockmining import live as LV
    from stockmining import analyst as AI
    from stockmining.config import load_config
    from stockmining.jobs import App
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"market": "kr", "notifier": "console", "db_path": str(tmp_path / "k.db"),
                                 "principles_path": str(tmp_path / "p.md")}))
    app = App(load_config(p))
    assert app.playbook() == []  # nothing before the mock investing starts
    st = LV.new_state(app, "2026-11-05")
    for m in st["methods"].values():
        m["eq"].append(["2026-11-06", 1.01])
    st["last"] = "2026-11-06"
    LV.save(app.db, st)
    seen = {}

    def fake(cfg, principles, data):
        seen["prev"] = data["previous"]
        return {"headline": "아직 이르다", "works": [], "fails": [], "my_rules": [{"rule": "작게 시작", "why": "표본 부족"}],
                "watch": [], "changes": ["첫 노트"], "new_insights": [], "insight_updates": []}
    monkeypatch.setattr(AI, "playbook", fake)
    monkeypatch.setattr(App, "claude_ready", property(lambda self: True))
    lines = app.playbook()
    assert "투자 노트 · 직접 투자 준비 " in lines[0] and "아직 이르다" in lines
    app.playbook()
    assert seen["prev"]["headline"] == "아직 이르다"  # second week starts from the first note
    from stockmining import web
    from stockmining import quotes as Q
    monkeypatch.setattr(Q, "get", lambda codes: {})
    assert "작게 시작" in web.route(app.cfg, app.db, "/reports", {"t": ["notes"]})



def test_news_parse_tag_and_point_in_time(tmp_path, monkeypatch):
    from stockmining import news as NW
    rss = """<?xml version="1.0"?><rss><channel><title>Google 뉴스</title>
      <item><title>OO전자, 3000억 유상증자 결정 - 연합뉴스</title><link>https://x/1</link>
        <pubDate>Thu, 05 Nov 2026 07:00:00 GMT</pubDate><source url="https://yna">연합뉴스</source></item>
      <item><title>OO전자 대규모 수주 공급계약 체결</title><link>https://x/2</link></item></channel></rss>"""

    class R:
        content = rss.encode()
        def raise_for_status(self): pass
    monkeypatch.setattr(NW.requests, "get", lambda *a, **k: R())
    items = NW._rss("https://example")
    assert items[0]["title"] == "OO전자, 3000억 유상증자 결정" and items[0]["source"] == "연합뉴스"
    assert items[0]["published"].startswith("2026-11-05T07:00")
    assert NW.rule_tags(items[0]["title"]) == ["capital_raise"] and "contract" in NW.rule_tags(items[1]["title"])
    assert NW.rule_sentiment(items[0]["title"], ["capital_raise"]) < 0 < NW.rule_sentiment(items[1]["title"], ["contract"])
    db = DB(tmp_path / "n.db")
    NW.ensure(db)
    NW._insert(db, "000001", "news", items[1:], "2026-11-05T18:00:00+09:00")
    NW._insert(db, "000001", "news", items[:1], "2026-11-06T18:00:00+09:00")
    db.execute("UPDATE news SET sentiment=1 WHERE title NOT LIKE '%유상증자%'")
    db.execute("UPDATE news SET sentiment=-1 WHERE title LIKE '%유상증자%'")
    NW._insert(db, "000002", "news", [{"title": "KB금융 신종자본증권 모집에 4710억 몰려"}], "2026-11-06T18:00:00+09:00")
    db.execute("UPDATE news SET sentiment=1, tags='[\"capital_raise\"]' WHERE code='000002'")
    assert NW.snapshot(db, "000002", "2026-11-07T08:00:00+09:00")["label"] == "good"  # good news, not a dilution risk
    before = NW.snapshot(db, "000001", "2026-11-06T08:00:00+09:00")
    after = NW.snapshot(db, "000001", "2026-11-07T08:00:00+09:00")
    assert before["n"] == 1 and before["label"] == "good"     # the 유상증자 item wasn't seen yet
    assert after["n"] == 2 and after["label"] == "risk"
    assert NW.snapshot(db, "999999", "2026-11-07T08:00:00+09:00")["label"] == "none"
    assert NW.snapshot(DB(tmp_path / "empty.db"), "x", "2026-11-07T08:00:00+09:00")["label"] == "unknown"


def test_news_pages_render(tmp_path, monkeypatch):
    from stockmining import news as NW
    from stockmining import web
    from stockmining import quotes as Q
    from stockmining.config import load_config
    monkeypatch.setattr(Q, "get", lambda codes: {})
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"market": "kr", "notifier": "console", "db_path": str(tmp_path / "k.db")}))
    cfg = load_config(p)
    db = DB(cfg.path("db_path"))
    NW.ensure(db)
    NW._insert(db, "", "news", [{"title": "코스피 <급락>", "source": "연합", "link": "javascript:alert(1)"}], "2026-11-06T18:00:00+09:00")
    db.execute("UPDATE news SET sentiment=-2")
    home = web.route(cfg, db, "/", {})
    assert "시장 뉴스" in home and "&lt;급락&gt;" in home and "javascript:" not in home
    assert "코스피" in web.route(cfg, db, "/news", {})

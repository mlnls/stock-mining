"""Forward mock investing from a fixed start day: 10,000,000 (virtual) per method, played for real going forward.

Unlike replay.py (which re-plays the past), this is a ledger that only ever moves forward:
  * it starts on the day it was created; the first buys are at the NEXT trading day's open, from signals
    on the start day's close — nothing before "now" is ever traded;
  * every evening it processes only the trading days it has not seen yet, then saves; a day once
    processed is never rewritten, even if later data or a retrained model would have decided differently;
  * each method picks trades exactly as in replay.py (same rules, same quarterly strategy choice, same
    equal slots), and a held position exits by its own strategy's stop / target / trailing / time rule.

State is JSON in kv["live"] (per market DB).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time as dtime

import numpy as np
import pandas as pd
import yaml

from . import backtest as BT
from . import features as F
from . import rules as R
from . import replay as RP

log = logging.getLogger(__name__)
KEY = "live"
AMOUNT = 10_000_000
PICK = "best"  # the method Claude would run with its own money (best in both markets' no-hindsight replay)


def load(db) -> dict | None:
    raw = db.get(KEY)
    return json.loads(raw) if raw else None


def save(db, st: dict) -> None:
    db.set(KEY, json.dumps(st, ensure_ascii=False, separators=(",", ":")))


def new_state(app, start: str | None = None) -> dict:
    start = start or app.today()
    return {
        "start": start, "last": start, "amount": AMOUNT, "pick": PICK, "market": app.cfg["market"],
        "bench": {"name": {"US500": "S&P500", "KS11": "코스피"}.get(app.cfg["regime_index"], app.cfg["regime_index"]),
                  "base": None, "eq": [[start, 1.0]]},
        "methods": {m["id"]: {"label": m["label"], "desc": m["desc"], "slots": m["slots"], "cash": 1.0,
                              "pos": [], "events": [], "eq": [[start, 1.0]]} for m in RP.METHODS},
        "names": {},
    }


def _specs(app) -> dict[str, dict]:
    out = {}
    for f in sorted(app.cfg.path("strategies_dir").glob("*.yaml")):
        spec = R.resolve(R.validate(yaml.safe_load(f.read_text(encoding="utf-8"))))
        out[spec["name"]] = spec
    return out


def _news_at(app, code: str, day: str) -> str:
    """News label known before that day's open (collected the previous evening or earlier)."""
    from . import news as NW
    asof = datetime.combine(date.fromisoformat(day), dtime(8, 0), tzinfo=app.tz).isoformat(timespec="seconds")
    return NW.snapshot(app.db, code, asof)["label"]


def _exit(df: pd.DataFrame, spec: dict, p: dict, costs: dict):
    """Where this position's own exit rule closes it, using bars known so far: (date, price, ret%, reason) or None."""
    idx = df.index
    e = idx.get_loc(pd.Timestamp(p["entry_date"]))
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    atr = F.compute_feature(df, "atr_14").to_numpy()
    res = R.simulate_exit(o, h, l, c, atr, R.exit_when_mask(df, spec), e, p["entry"], p["stop"], p["target"],
                          spec["exit"], intrabar_entry=spec["entry"].get("type") == "breakout")
    if res.idx is None:
        return None
    return idx[res.idx], float(res.price), BT.net_return(p["entry"], float(res.price), costs), res.reason


def update(app, ctx=None) -> dict | None:
    """Advance every method through the trading days not processed yet. Returns the saved state."""
    st = load(app.db)
    if st is None:
        st = new_state(app)
        save(app.db, st)
        log.info("live mock investing started %s", st["start"])
    ctx = ctx or app.full_context()
    panel, regs, idx, info = ctx
    bench = idx.get(app.cfg["regime_index"])
    if bench is None or not len(bench):
        log.warning("live: no benchmark data")
        return st
    last = pd.Timestamp(st["last"])
    days = bench.index[bench.index > last]
    if st["bench"]["base"] is None and bench.index[-1] >= last:  # start day's close (or the last one before it)
        st["bench"]["base"] = float(bench["close"].loc[:last].iloc[-1])
    if not len(days):
        save(app.db, st)
        return st

    tbs = RP.yaml_trades(app, ctx)
    specs = _specs(app)
    costs = app.cfg["costs"]
    q0 = pd.Timestamp(days[0]).to_period("Q").start_time  # selection for a quarter uses trades closed before it
    end = pd.Timestamp(days[-1]) + pd.Timedelta(days=1)
    chosen = {m["id"]: RP.choose(tbs, q0, end, m) for m in RP.METHODS}
    closes = RP._Closes(panel)

    for d in days:
        ds = d.strftime("%Y-%m-%d")
        for m in RP.METHODS:
            S = st["methods"].setdefault(m["id"], {"label": m["label"], "desc": m["desc"], "slots": m["slots"],
                                                   "cash": 1.0, "pos": [], "events": [], "eq": [[st["start"], 1.0]]})
            # 1) exits by each position's own rule, with bars up to d
            keep = []
            for p in S["pos"]:
                df, spec = panel.get(p["code"]), specs.get(p["strategy"])
                x = _exit(df, spec, p, costs) if df is not None and spec is not None else None
                if x and x[0] <= d:
                    S["cash"] += p["alloc"] * (1 + x[2] / 100)
                    S["events"].append([x[0].strftime("%Y-%m-%d"), "s", p["code"], p["strategy"], round(x[1], 4),
                                        round(x[2], 2), x[3], p["entry_date"], p.get("regime", ""), p.get("news", "unknown")])
                else:
                    keep.append(p)
            S["pos"] = keep
            total = S["cash"] + sum(p["alloc"] * p["last"] / p["entry"] for p in S["pos"])
            # 2) buys at today's open from yesterday's signals, best score first, equal slots
            for t in sorted((t for t in chosen[m["id"]] if t.entry_date == d), key=lambda t: -t.score):
                if len(S["pos"]) >= m["slots"] or S["cash"] <= 1e-9:
                    break
                if any(p["code"] == t.code for p in S["pos"]) or t.strategy not in specs:
                    continue
                df = panel[t.code]
                atr = F.compute_feature(df, "atr_14").to_numpy()
                stop, target = R.stop_target(t.entry, atr[df.index.get_loc(t.signal_date)], specs[t.strategy]["exit"])
                alloc = min(total / m["slots"], S["cash"])
                S["cash"] -= alloc
                S["pos"].append({"code": t.code, "strategy": t.strategy, "signal_date": t.signal_date.strftime("%Y-%m-%d"),
                                 "entry_date": ds, "entry": float(t.entry), "stop": float(stop),
                                 "target": float(target) if target is not None else None, "alloc": alloc, "last": float(t.entry),
                                 "regime": t.regime, "news": _news_at(app, t.code, ds)})
                S["events"].append([ds, "b", t.code, t.strategy, round(float(t.entry), 4), None, ""])
                st["names"][t.code] = (info.get(t.code, {}) or {}).get("name") or t.code
            # 3) value at today's close
            for p in S["pos"]:
                c = closes(p["code"]).get(d)
                if c is not None and np.isfinite(c):
                    p["last"] = float(c)
            S["eq"].append([ds, round(S["cash"] + sum(p["alloc"] * p["last"] / p["entry"] for p in S["pos"]), 6)])
        if st["bench"]["base"]:
            st["bench"]["eq"].append([ds, round(float(bench["close"].loc[d]) / st["bench"]["base"], 6)])
        st["last"] = ds
    save(app.db, st)
    log.info("live: processed %s day(s) up to %s", len(days), st["last"])
    return st


def summary(st: dict | None) -> list[dict]:
    """Methods ranked by current value (ratio), with the benchmark as a reference row."""
    if not st:
        return []
    rows = [{"id": k, "label": v["label"], "eq": v["eq"][-1][1], "n": len(v["pos"]),
             "trades": sum(1 for e in v["events"] if e[1] == "b"), "pick": k == st.get("pick")}
            for k, v in st["methods"].items()]
    rows.sort(key=lambda r: -r["eq"])
    return rows


def slack_lines(st: dict | None, cur_fmt=lambda v: f"{v:,.0f}원") -> str:
    """Short evening note: today's trades of Claude's pick + the league table."""
    if not st:
        return ""
    amt, rows = st["amount"], summary(st)
    days = len(st["methods"][rows[0]["id"]]["eq"]) - 1 if rows else 0
    out = [f"📒 *지금부터 모의투자* · {st['start']} 시작 · {days}거래일째 (각 {amt // 10_000:,}만원)"]
    P = st["methods"].get(st.get("pick"))
    if P:
        today = [e for e in P["events"] if e[0] == st["last"]]
        for e in today:
            nm = st["names"].get(e[2], e[2])
            out.append(f"• {'🟢 샀어요' if e[1] == 'b' else '🔴 팔았어요'} {nm}" + (f" ({e[5]:+.1f}%)" if e[1] == "s" else ""))
    for r in rows:
        out.append(f"{'⭐ ' if r['pick'] else '• '}{r['label']}  {cur_fmt(amt * r['eq'])} ({(r['eq'] - 1) * 100:+.1f}%)")
    b = st["bench"]["eq"][-1][1] if st["bench"]["eq"] else None
    if b:
        out.append(f"• {st['bench']['name']} 그냥 들고 있기  {(b - 1) * 100:+.1f}%")
    return "\n".join(out)


# ---------------------------------------------------------------- what the forward results teach (for the user)
READY_DAYS, READY_TRADES, READY_MDD = 60, 30, 20.0


def _mdd(v: list[float]) -> float:
    a = np.array(v, dtype=float)
    return float(-((a / np.maximum.accumulate(a)) - 1).min() * 100) if len(a) else 0.0


def stats(st: dict | None, backtests: dict | None = None) -> dict | None:
    """Per method (return, drawdown, vs index) and per strategy (closed trades, pooled over methods without
    double counting): what has actually worked since the start day. ``backtests`` = {name: expectancy%}."""
    if not st:
        return None
    b = [p[1] for p in st["bench"]["eq"]]
    bench = (b[-1] - 1) * 100 if b else None
    methods = []
    for k, v in st["methods"].items():
        eq = [p[1] for p in v["eq"]]
        sells = [e for e in v["events"] if e[1] == "s"]
        r = np.array([e[5] for e in sells], dtype=float)
        methods.append({"id": k, "label": v["label"], "pick": k == st.get("pick"), "total_pct": round((eq[-1] - 1) * 100, 2),
                        "mdd_pct": round(_mdd(eq), 2), "closed": len(sells), "open": len(v["pos"]),
                        "win_rate": round(float((r > 0).mean() * 100), 1) if len(r) else None,
                        "avg_pct": round(float(r.mean()), 2) if len(r) else None,
                        "vs_bench_pct": round((eq[-1] - 1) * 100 - bench, 2) if bench is not None else None})
    methods.sort(key=lambda m: -m["total_pct"])
    seen, by = set(), {}
    for v in st["methods"].values():
        for e in v["events"]:
            if e[1] != "s" or len(e) < 8:
                continue
            key = (e[2], e[3], e[7])
            if key in seen:
                continue
            seen.add(key)
            hold = (pd.Timestamp(e[0]) - pd.Timestamp(e[7])).days
            by.setdefault(e[3], []).append({"ret": e[5], "why": e[6], "hold": hold, "regime": e[8] or "neutral",
                                            "news": e[9] if len(e) > 9 else "unknown"})
    strategies = []
    for name, ts in by.items():
        r = np.array([t["ret"] for t in ts], dtype=float)
        why, reg, nws = {}, {}, {}
        for t in ts:
            why[t["why"]] = why.get(t["why"], 0) + 1
            reg.setdefault(t["regime"], []).append(t["ret"])
            nws.setdefault(t["news"], []).append(t["ret"])
        strategies.append({"name": name, "n": len(r), "win_rate": round(float((r > 0).mean() * 100), 1),
                           "avg_pct": round(float(r.mean()), 2), "best_pct": round(float(r.max()), 1),
                           "worst_pct": round(float(r.min()), 1), "hold_days": round(float(np.mean([t["hold"] for t in ts])), 1),
                           "exits": why, "by_regime": {k: [len(x), round(float(np.mean(x)), 2)] for k, x in reg.items()},
                           "by_news": {k: [len(x), round(float(np.mean(x)), 2)] for k, x in nws.items()},
                           "backtest_avg_pct": (backtests or {}).get(name)})
    strategies.sort(key=lambda s: -s["avg_pct"] * s["n"])
    news = {}  # does the news state at the time of buying tell anything? (all strategies pooled)
    for ts in by.values():
        for t in ts:
            news.setdefault(t["news"], []).append(t["ret"])
    by_news = {k: {"n": len(x), "avg_pct": round(float(np.mean(x)), 2), "win_rate": round(float((np.array(x) > 0).mean() * 100), 1)}
               for k, x in news.items()}
    days = len(next(iter(st["methods"].values()))["eq"]) - 1
    return {"start": st["start"], "last": st["last"], "days": days, "bench_name": st["bench"]["name"],
            "bench_pct": round(bench, 2) if bench is not None else None, "methods": methods, "strategies": strategies,
            "by_news": by_news}


def readiness(S: dict | None, pick: str = PICK) -> list[dict]:
    """Checklist before the user puts real money behind the starred method."""
    if not S:
        return []
    m = next((x for x in S["methods"] if x["id"] == pick), None)
    if not m:
        return []
    return [
        {"label": f"{READY_DAYS}거래일(약 3개월) 이상 돌려 봤다", "ok": S["days"] >= READY_DAYS, "now": f"{S['days']}일"},
        {"label": f"끝난 거래가 {READY_TRADES}번 이상이다 (운으로 보기 어려운 수)", "ok": m["closed"] >= READY_TRADES,
         "now": f"{m['closed']}번"},
        {"label": f"{S['bench_name']}를 그냥 들고 있는 것보다 많이 벌었다",
         "ok": m["vs_bench_pct"] is not None and m["vs_bench_pct"] > 0 and S["days"] > 0,
         "now": f"{m['vs_bench_pct']:+.1f}%p" if m["vs_bench_pct"] is not None else "-"},
        {"label": f"고점 대비 가장 많이 빠진 게 {READY_MDD:.0f}% 이내다", "ok": m["mdd_pct"] <= READY_MDD and S["days"] > 0,
         "now": f"-{m['mdd_pct']:.1f}%"},
        {"label": "끝난 거래의 평균 수익이 비용을 빼고도 플러스다", "ok": (m["avg_pct"] or 0) > 0,
         "now": f"{m['avg_pct']:+.2f}%" if m["avg_pct"] is not None else "-"},
    ]


def brief(st: dict | None, backtests: dict | None = None) -> dict | None:
    """Compact view for Claude's prompts."""
    S = stats(st, backtests)
    if not S:
        return None
    P = st["methods"].get(st.get("pick"), {})
    return {**S, "pick": st.get("pick"), "readiness": readiness(S),
            "pick_holdings": [{k: p[k] for k in ("code", "strategy", "entry_date", "entry", "stop", "last")} for p in P.get("pos", [])],
            "pick_recent": P.get("events", [])[-10:],
            "note": "앞으로 진행 중인 실전 모의투자(결과를 보고 고칠 수 없는 기록). 표본이 적으면 결론을 미룰 것."}

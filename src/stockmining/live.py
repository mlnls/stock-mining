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
                                        round(x[2], 2), x[3]])
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
                                 "target": float(target) if target is not None else None, "alloc": alloc, "last": float(t.entry)})
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

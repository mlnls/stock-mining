"""Time-machine mock investing: replay the past day by day as if the future were unknown.

Every investing method starts with the same virtual money on the same day and, on each trading day, may
only use what was known by that day's close:
  * signals come from rules evaluated on bars up to that day (entry at the NEXT open);
  * the "learning" methods re-pick strategies every quarter using only trades that had already CLOSED;
  * ``ml_score`` is itself walk-forward (see ml.py), so the AI strategy never saw its test period.
Positions are marked to market at each close, so the curve shows the drawdowns you would have lived through.

Only methods that could really have been run are included: nothing is picked by its final result.
Rule parameters are the YAML defaults (from each strategy's source), not values later tuned on the full
history, so tuning cannot leak the future either.

Result is stored as JSON in kv["replay"] and played back by the dashboard at /sim.
"""
from __future__ import annotations

import json
import logging
import time

import numpy as np
import pandas as pd

from . import backtest as BT
from . import labels as LB
from . import regime as RG
from . import simulate as SIM

log = logging.getLogger(__name__)
AMOUNT = 10_000_000  # virtual KRW shown on screen; curves are ratios so the market's currency does not matter
WINDOW_DAYS = 730    # learner looks back this far
RANDOM_RUNS = 60


METHODS = [
    {"id": "learner", "label": "AI 학습기", "slots": 5, "policy": "learner",
     "desc": "분기마다 지난 2년간 끝난 거래만 보고, 지금 시장 분위기에서 돈을 번 전략만 골라 5종목까지 담아요. "
             "지금 실제 추천에 쓰는 방식과 같아요."},
    {"id": "learner10", "label": "AI 학습기 · 넓게", "slots": 10, "policy": "learner",
     "desc": "AI 학습기와 같은데 10종목으로 나눠 담아요."},
    {"id": "best", "label": "분기 1등 따라가기", "slots": 5, "policy": "top", "k": 1,
     "desc": "분기마다 지난 2년 성적이 가장 좋았던 전략 하나만 써요."},
    {"id": "top3", "label": "분기 상위 3개 전략", "slots": 5, "policy": "top", "k": 3,
     "desc": "분기마다 지난 2년 성적 상위 3개 전략만 써요."},
    {"id": "equal", "label": "전략 전부 섞기", "slots": 10, "policy": "all", "desc": "고르지 않고 모든 전략 신호를 점수 순으로 10종목까지 담아요."},
]


def yaml_trades(app, ctx) -> dict[str, list[BT.Trade]]:
    """Every built-in strategy with its ORIGINAL YAML params; scores → daily percentile within the strategy."""
    import yaml
    from . import rules as R
    panel, regs, _, _ = ctx
    out = {}
    for f in sorted(app.cfg.path("strategies_dir").glob("*.yaml")):
        spec = R.resolve(R.validate(yaml.safe_load(f.read_text(encoding="utf-8"))))
        BT.prepare_panel(panel, [spec])
        trades = BT.run(panel, spec, app.cfg["costs"], regs, min_value=app.min_value)
        if trades:
            df = pd.DataFrame({"d": [t.signal_date for t in trades], "s": [t.score for t in trades]})
            pct = df.groupby("d")["s"].rank(pct=True).to_numpy()
            trades = [BT.Trade(**{**t.__dict__, "score": float(p)}) for t, p in zip(trades, pct)]
        out[spec["name"]] = trades
    return out


def choose(tbs, start, end, m: dict) -> list[BT.Trade]:
    """Quarter by quarter, pick strategies from trades closed before the quarter began."""
    if m["policy"] == "learner":
        return SIM.walk_forward(tbs, start, end, "learner", WINDOW_DAYS)
    if m["policy"] == "all":
        return SIM.walk_forward(tbs, start, end, "all_equal", WINDOW_DAYS)
    chosen = []
    edges = list(pd.date_range(start, end, freq="QS")) + [end]
    for qs, qe in zip(edges[:-1], edges[1:]):
        stats = {n: SIM._trailing_expectancy(tr, qs, WINDOW_DAYS) for n, tr in tbs.items()}
        good = sorted((n for n, st in stats.items() if st and st["overall"] > 0), key=lambda n: -stats[n]["overall"])
        for n in good[:m["k"]]:
            chosen += [t for t in tbs[n] if qs <= t.signal_date < qe]
    return chosen


class _Closes:
    def __init__(self, panel):
        self.panel, self.cache = panel, {}

    def __call__(self, code: str) -> dict:
        if code not in self.cache:
            s = self.panel[code]["close"]
            self.cache[code] = dict(zip(s.index, s.to_numpy(dtype=float)))
        return self.cache[code]


def mark_to_market(trades: list[BT.Trade], slots: int, dates: pd.DatetimeIndex, closes: _Closes,
                   events: bool = True) -> tuple[np.ndarray, list, list[BT.Trade]]:
    """Daily equity (start 1.0) of an equal-slot portfolio that fills free slots with the best-scored
    new trades each day. Same selection as backtest.portfolio, but open positions are valued at the close."""
    by_entry: dict[pd.Timestamp, list[BT.Trade]] = {}
    for t in trades:
        by_entry.setdefault(t.entry_date, []).append(t)
    cash, eq = 1.0, np.empty(len(dates))
    pos: list[dict] = []  # {t, alloc, last}
    ev, taken = [], []
    for di, d in enumerate(dates):
        keep = []
        for p in pos:
            t = p["t"]
            if t.exit_date <= d and t.reason != "open":
                cash += p["alloc"] * (1 + t.ret_pct / 100)
                if events:
                    ev.append([di, "s", t.code, t.strategy, round(t.exit, 4), round(t.ret_pct, 2), t.reason])
            else:
                keep.append(p)
        pos = keep
        total = cash + sum(p["alloc"] * p["last"] / p["t"].entry for p in pos)
        for t in sorted(by_entry.get(d, []), key=lambda t: -t.score):
            if len(pos) >= slots or cash <= 1e-9:
                break
            if any(p["t"].code == t.code for p in pos):
                continue
            alloc = min(total / slots, cash)
            cash -= alloc
            pos.append({"t": t, "alloc": alloc, "last": t.entry})
            taken.append(t)
            if events:
                ev.append([di, "b", t.code, t.strategy, round(t.entry, 4), None, ""])
        for p in pos:
            c = closes(p["t"].code).get(d)
            if c is not None and np.isfinite(c):
                p["last"] = c
        eq[di] = cash + sum(p["alloc"] * p["last"] / p["t"].entry for p in pos)
    return eq, ev, taken


def _stats(eq: np.ndarray, dates: pd.DatetimeIndex, taken: list[BT.Trade]) -> dict:
    s = pd.Series(eq, index=dates)
    years = max((dates[-1] - dates[0]).days / 365.25, 1e-9)
    closed = [t for t in taken if t.reason != "open"]
    r = np.array([t.ret_pct for t in closed]) if closed else np.array([])
    monthly = s.resample("ME").last().pct_change().dropna()
    return {
        "total_pct": round(float((eq[-1] - 1) * 100), 2),
        "cagr_pct": round(float((eq[-1] ** (1 / years) - 1) * 100), 2) if eq[-1] > 0 else -100.0,
        "mdd_pct": round(float(-((s / s.cummax()) - 1).min() * 100), 2),
        "trades": len(taken), "closed": len(closed),
        "win_rate": round(float((r > 0).mean() * 100), 1) if len(r) else None,
        "avg_pct": round(float(r.mean()), 2) if len(r) else None,
        "best_pct": round(float(r.max()), 1) if len(r) else None,
        "worst_pct": round(float(r.min()), 1) if len(r) else None,
        "pos_month_pct": round(float((monthly > 0).mean() * 100), 0) if len(monthly) else None,
        "worst_month_pct": round(float(monthly.min() * 100), 1) if len(monthly) else None,
    }


def _learning_log(tbs, edges, dates, regime: pd.Series) -> list[dict]:
    out = []
    for qs in edges:
        di = int(dates.searchsorted(qs))
        if di >= len(dates):
            continue
        rg = str(regime.asof(qs)) if len(regime) else "neutral"
        rg = rg if rg in ("bull", "neutral", "bear") else "neutral"
        rows = []
        for name, tr in tbs.items():
            st = SIM._trailing_expectancy(tr, qs, WINDOW_DAYS)
            if not st:
                rows.append({"s": name, "n": 0, "exp": None, "now": None, "use": False})
                continue
            now = float(st["by_regime"][rg])
            rows.append({"s": name, "n": st["n"], "exp": round(float(st["overall"]), 2), "now": round(now, 2),
                         "use": now > 0})
        ok = [r for r in rows if r["exp"] is not None]
        best = max(ok, key=lambda r: r["exp"])["s"] if ok and max(r["exp"] for r in ok) > 0 else None
        rows.sort(key=lambda r: -(r["now"] if r["now"] is not None else -99))
        top3 = [r["s"] for r in sorted(ok, key=lambda r: -r["exp"]) if r["exp"] > 0][:3]
        out.append({"di": di, "date": qs.strftime("%Y-%m-%d"), "regime": rg, "rows": rows, "best": best, "top3": top3})
    return out


def build(app, ctx=None) -> dict:
    t0 = time.time()
    ctx = ctx or app.full_context()
    panel, regs, idx, info = ctx
    tbs = yaml_trades(app, ctx)
    start, last = SIM._bounds(ctx, WINDOW_DAYS)
    ri = app.cfg["regime_index"]
    bench = idx.get(ri)
    if bench is not None and len(bench):
        dates = bench.loc[(bench.index >= start) & (bench.index <= last)].index
    else:
        dates = pd.DatetimeIndex(sorted({d for df in panel.values() for d in df.index if start <= d <= last}))
    if len(dates) < 20:
        raise RuntimeError("replay window too short")
    closes = _Closes(panel)
    edges = list(pd.date_range(start, last, freq="QS"))
    names = sorted(tbs)

    methods = []
    for m in METHODS:
        chosen = choose(tbs, start, last, m)
        eq, ev, taken = mark_to_market(chosen, m["slots"], dates, closes)
        m = {k: v for k, v in m.items() if k not in ("policy", "k")}
        m.update(eq=[round(float(x), 4) for x in eq], events=ev, metrics=_stats(eq, dates, taken))
        methods.append(m)
        log.info("replay %-26s total=%s%% mdd=%s%% trades=%s", m["label"], m["metrics"]["total_pct"],
                 m["metrics"]["mdd_pct"], m["metrics"]["trades"])
    methods.sort(key=lambda m: -m["metrics"]["total_pct"])

    # luck baseline: same signals from every strategy, picked at random
    base = [t for tr in tbs.values() for t in tr if start <= t.signal_date < last]
    rng = np.random.default_rng(7)
    rand = []
    for _ in range(RANDOM_RUNS if base else 0):
        shuf = [BT.Trade(**{**t.__dict__, "score": float(x)}) for t, x in zip(base, rng.random(len(base)))]
        rand.append(float(mark_to_market(shuf, 5, dates, closes, events=False)[0][-1] - 1) * 100)

    reg = RG.regime_series(bench) if bench is not None and len(bench) else pd.Series(dtype=object)
    regime_str = "".join({"bull": "u", "bear": "d"}.get(str(reg.asof(d)) if len(reg) else "", "n") for d in dates)

    ml_retrain = []
    if "ml_rank" in tbs:
        from . import ml as ML
        all_dates = np.sort(pd.DatetimeIndex(sorted({d for df in panel.values() for d in df.index[-2000:]})))
        for k in range(ML.MIN_TRAIN, len(all_dates), ML.RETRAIN):
            d = pd.Timestamp(all_dates[k])
            if dates[0] <= d <= dates[-1]:
                ml_retrain.append(int(dates.searchsorted(d)))

    used = {e[2] for m in methods for e in m["events"]}
    out = {
        "ts": app.now().isoformat(timespec="seconds"),
        "start": dates[0].strftime("%Y-%m-%d"), "end": dates[-1].strftime("%Y-%m-%d"),
        "amount": AMOUNT, "market": app.cfg["market"], "cur": "USD" if app.cfg.is_us else "KRW",
        "dates": [d.strftime("%Y-%m-%d") for d in dates],
        "regime": regime_str,
        "bench": {"name": {"^GSPC": "S&P500", "US500": "S&P500", "KS11": "코스피", "KQ11": "코스닥"}.get(ri, ri),
                  "eq": ([round(float(x), 4) for x in (bench["close"].reindex(dates).ffill() / bench["close"].reindex(dates).ffill().iloc[0])]
                         if bench is not None and len(bench) else None)},
        "methods": methods,
        "learn": _learning_log(tbs, edges, dates, reg),
        "ml_retrain": ml_retrain,
        "random": ({"runs": len(rand), "median_pct": round(float(np.median(rand)), 1),
                    "p10_pct": round(float(np.percentile(rand, 10)), 1), "p90_pct": round(float(np.percentile(rand, 90)), 1)}
                   if rand else None),
        "names": {c: (info.get(c, {}).get("name") or c) for c in used},
        "strat_ko": {n: LB.strat(n) for n in names},
        "secs": round(time.time() - t0),
    }
    if out["bench"]["eq"]:
        b = np.array(out["bench"]["eq"])
        s = pd.Series(b, index=dates)
        out["bench"]["metrics"] = {"total_pct": round(float((b[-1] - 1) * 100), 2),
                                   "mdd_pct": round(float(-((s / s.cummax()) - 1).min() * 100), 2)}
    log.info("replay built in %ss: %s days, %s methods", out["secs"], len(dates), len(methods))
    return out


def run(app, ctx=None) -> dict:
    out = build(app, ctx)
    app.db.set("replay", json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    return out

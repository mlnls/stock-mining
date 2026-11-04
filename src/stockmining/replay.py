"""Time-machine mock investing: replay the past day by day as if the future were unknown.

Every investing method starts with the same virtual money on the same day and, on each trading day, may
only use what was known by that day's close:
  * signals come from rules evaluated on bars up to that day (entry at the NEXT open);
  * the "learning" methods re-pick strategies every quarter using only trades that had already CLOSED;
  * ``ml_score`` is itself walk-forward (see ml.py), so the AI strategy never saw its test period.
Positions are marked to market at each close, so the curve shows the drawdowns you would have lived through.

Caveat kept on screen: the single-strategy rows are hindsight comparisons (nobody knew in advance which
one would win), and rule parameters were chosen from the full history; the learning methods are the
honest "what would actually have happened" numbers.

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
SINGLE_SLOTS = 5
RANDOM_RUNS = 60


def _methods(app, names: list[str]) -> list[dict]:
    from .config import PROFILES
    aggr = [n for n in PROFILES["aggressive"]["strategy_include"] if n in names]
    out = [
        {"id": "learner", "label": "AI 학습기", "slots": 5, "policy": "learner", "pool": None, "kind": "system",
         "desc": "분기마다 지난 2년간 끝난 거래만 보고, 지금 시장 분위기에서 돈을 번 전략만 골라 써요. "
                 "실제로 매일 돌아가는 방식과 같아요."},
        {"id": "best", "label": "분기 1등 따라가기", "slots": 5, "policy": "best_trailing", "pool": None, "kind": "system",
         "desc": "분기마다 지난 2년 성적이 가장 좋았던 전략 하나만 써요."},
        {"id": "equal", "label": "전략 전부 섞기", "slots": 10, "policy": "all_equal", "pool": None, "kind": "system",
         "desc": "고르지 않고 모든 전략 신호를 점수 순으로 10종목까지 담아요."},
        {"id": "aggr", "label": "공격형 4전략", "slots": 3, "policy": "all_equal", "pool": aggr, "kind": "fixed",
         "desc": "추세·돌파 전략 4개(" + ", ".join(LB.strat(n) for n in aggr) + ")만, 3종목에 집중해요. "
                 "백테스트 성적을 보고 고른 묶음이라 지나고 나서 고른 선택이에요."},
    ]
    for n in names:
        out.append({"id": f"s:{n}", "label": LB.strat(n), "slots": SINGLE_SLOTS, "policy": "all_equal", "pool": [n],
                    "kind": "single", "strategy": n, "desc": f"{LB.strat(n)} 전략 하나만 계속 썼을 때예요."})
    return out


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
        out.append({"di": di, "date": qs.strftime("%Y-%m-%d"), "regime": rg, "rows": rows, "best": best})
    return out


def build(app, ctx=None) -> dict:
    t0 = time.time()
    ctx = ctx or app.full_context()
    panel, regs, idx, info = ctx
    tbs = SIM.all_trades(app, ctx)
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
    for m in _methods(app, names):
        pool = {k: v for k, v in tbs.items() if not m["pool"] or k in m["pool"]}
        if not pool:
            continue
        chosen = SIM.walk_forward(pool, start, last, m["policy"], WINDOW_DAYS)
        eq, ev, taken = mark_to_market(chosen, m["slots"], dates, closes)
        m = {k: v for k, v in m.items() if k not in ("policy", "pool")}
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

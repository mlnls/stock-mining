"""Edge map: *where* does each strategy make or lose money?

For every strategy's trades (recent backtest window + closed paper trades), look up market/stock
conditions on the signal day, split each condition into terciles, and compare expectancy per bucket
with the strategy's overall expectancy. Large, well-sampled gaps are candidate filters — Claude turns
them into rule proposals, which still have to pass the backtest gate.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import backtest as BT
from . import features as F
from . import rules as R

# conditions measured at the signal close (all computable from daily bars)
CONDITIONS = {
    "natr_14": "변동성(ATR %)",
    "xrank_ret_60": "60일 상대강도 순위",
    "xrank_ret_250": "12개월 상대강도 순위",
    "vol_ratio_20": "거래량 배수(20일 대비)",
    "ret_5": "최근 5일 수익률",
    "ext_50": "50일선 대비 이격(%)",
    "value_rank": "거래대금 순위",
}


def _condition_series(df: pd.DataFrame, name: str) -> pd.Series:
    if name == "ext_50":
        return (df["close"] / F.compute_feature(df, "sma_50") - 1) * 100
    if name == "value_rank":
        return df.get("xrank_vavg_20", pd.Series(np.nan, index=df.index))
    return F.compute_feature(df, name)


def trade_frame(panel, trades: list[BT.Trade]) -> pd.DataFrame:
    rows = []
    for t in trades:
        df = panel.get(t.code)
        if df is None or t.signal_date not in df.index:
            continue
        row = {"ret": t.ret_pct, "regime": t.regime, "weekday": t.signal_date.weekday(), "date": t.signal_date}
        for c in CONDITIONS:
            row[c] = _condition_series(df, c).get(t.signal_date, np.nan)
        rows.append(row)
    return pd.DataFrame(rows)


def analyze(frame: pd.DataFrame, min_n: int = 30) -> list[dict]:
    """Per-condition tercile expectancy vs overall; returns findings sorted by impact."""
    if len(frame) < 3 * min_n:
        return []
    overall = frame["ret"].mean()
    out = []
    for c, label in [*CONDITIONS.items(), ("regime", "시장 국면")]:
        s = frame[c]
        if c == "regime":
            groups = {k: g for k, g in frame.groupby("regime")}
        else:
            if s.notna().sum() < 3 * min_n:
                continue
            try:
                q = pd.qcut(s, 3, labels=["low", "mid", "high"], duplicates="drop")
            except ValueError:
                continue
            groups = {}
            for k, g in frame.groupby(q, observed=True):
                lo, hi = s[g.index].min(), s[g.index].max()
                groups[f"{k} [{lo:.3g}~{hi:.3g}]"] = g
        for k, g in groups.items():
            if len(g) < min_n:
                continue
            r = g["ret"]
            wins, losses = r[r > 0].sum(), -r[r <= 0].sum()
            # t-stat of the bucket mean vs overall: filters out noise
            se = r.std(ddof=1) / np.sqrt(len(r)) if len(r) > 1 else np.inf
            out.append({
                "condition": c, "label": label, "bucket": str(k), "n": int(len(r)),
                "expectancy_pct": round(float(r.mean()), 2), "overall_pct": round(float(overall), 2),
                "delta_pct": round(float(r.mean() - overall), 2),
                "profit_factor": round(float(wins / losses), 2) if losses > 0 else 99.0,
                "t_stat": round(float((r.mean() - overall) / se), 2) if se and np.isfinite(se) else 0.0,
            })
    return sorted(out, key=lambda x: -abs(x["t_stat"]))


def build(app, ctx=None, lookback_days: int = 900, top: int = 6) -> dict:
    """Edge map for every live strategy. Returns {strategy: [top findings]} (|t| ≥ 2 only)."""
    ctx = ctx or app.full_context()
    panel, regs, _, _ = ctx
    F.add_cross_sectional(panel, ["xrank_ret_60 > 0", "xrank_ret_250 > 0", "xrank_vavg_20 > 0"])
    last = max(df.index[-1] for df in panel.values() if len(df))
    since = last - pd.Timedelta(days=lookback_days)
    result = {}
    for st in app.db.strategies(("active", "muted")):
        spec = R.resolve(st["spec"])
        BT.prepare_panel(panel, [spec])
        trades = BT.run(panel, spec, app.cfg["costs"], regs, start=since, min_value=app.min_value)
        frame = trade_frame(panel, trades)
        paper = app.db.df("SELECT code, signal_date, ret_pct, regime FROM paper_trades "
                          "WHERE strategy=? AND status='closed'", (st["name"],))
        if not paper.empty:  # live evidence counts too
            ptr = [BT.Trade(r.code, pd.Timestamp(r.signal_date), pd.Timestamp(r.signal_date), pd.Timestamp(r.signal_date),
                            0, 0, "paper", r.ret_pct, 0, r.regime or "neutral", 0) for r in paper.itertuples()]
            frame = pd.concat([frame, trade_frame(panel, ptr)], ignore_index=True)
        findings = [f for f in analyze(frame) if abs(f["t_stat"]) >= 2][:top]
        result[st["name"]] = {"trades": int(len(frame)), "findings": findings}
    return result


def summarize(edge: dict) -> str:
    lines = []
    for name, v in edge.items():
        for f in v["findings"][:3]:
            sign = "▲" if f["delta_pct"] > 0 else "▼"
            lines.append(f"{sign} {name}: {f['label']} {f['bucket']} → 거래당 {f['expectancy_pct']}% "
                         f"(전체 {f['overall_pct']}%, {f['n']}건, t={f['t_stat']})")
    return "\n".join(lines) or "통계적으로 뚜렷한 조건 차이 없음"


# ---------------------------------------------------------------------------- findings → filter proposals
_DSL = {
    "natr_14": "natr_14", "xrank_ret_60": "xrank_ret_60", "xrank_ret_250": "xrank_ret_250",
    "vol_ratio_20": "vol_ratio_20", "ret_5": "ret_5", "ext_50": "(close / sma_50 * 100 - 100)",
    "value_rank": "xrank_vavg_20",
}


def filter_from_finding(f: dict) -> str | None:
    """Losing low/high tercile → exclude it; winning low/high tercile → restrict to it. Mid buckets skipped."""
    if f["condition"] not in _DSL:
        return None
    side = f["bucket"].split(" ")[0]
    try:
        lo, hi = (float(x) for x in f["bucket"].split("[")[1].rstrip("]").split("~"))
    except (IndexError, ValueError):
        return None
    expr = _DSL[f["condition"]]
    if f["delta_pct"] < 0:
        return {"low": f"{expr} > {hi:.4g}", "high": f"{expr} < {lo:.4g}"}.get(side)
    return {"low": f"{expr} <= {hi:.4g}", "high": f"{expr} >= {lo:.4g}"}.get(side)


def refinements(app, edge: dict, ctx=None, max_per_strategy: int = 2) -> list[dict]:
    """Try each strong finding as an extra entry condition; keep those that pass the gate AND beat the
    current rule both in-sample and out-of-sample. Returns proposal dicts (not yet stored)."""
    ctx = ctx or app.full_context()
    gate = app.cfg["backtest"]["gate"]
    strategies = {s["name"]: s for s in app.db.strategies(("active", "muted"))}
    out = []
    for name, v in edge.items():
        st = strategies.get(name)
        if not st:
            continue
        base = app.evaluate(st["spec"], ctx)
        tried = 0
        for f in v["findings"]:
            cond = filter_from_finding(f)
            if not cond or cond in st["spec"]["entry"].get("all", []) or tried >= max_per_strategy:
                continue
            tried += 1
            spec = dict(st["spec"], entry={**st["spec"]["entry"], "all": [*st["spec"]["entry"].get("all", []), cond]})
            res = app.evaluate(spec, ctx)
            ok, why = BT.passes_gate(res, gate)
            better = (res["is"].get("expectancy_pct", -9) >= base["is"].get("expectancy_pct", -9) + 0.1
                      and res["oos"].get("profit_factor", 0) >= 0.95 * base["oos"].get("profit_factor", 0))
            out.append({"strategy": name, "condition": cond, "finding": f, "spec": spec, "result": res,
                        "baseline": base, "passed": ok and better, "why": why if not ok else ([] if better else ["기존 규칙보다 개선 없음"])})
    return out

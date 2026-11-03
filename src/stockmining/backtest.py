"""Backtesting: per-ticker trade simulation → portfolio simulation → metrics, plus walk-forward tuning."""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from . import features as F
from . import rules as R


@dataclass
class Trade:
    code: str
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry: float
    exit: float
    reason: str
    ret_pct: float     # net of costs
    score: float
    regime: str
    hold: int


def net_return(entry: float, exit: float, costs: dict) -> float:
    buy = entry * (1 + costs["commission"] + costs["slippage"])
    sell = exit * (1 - costs["commission"] - costs["sell_tax"] - costs["slippage"])
    return (sell / buy - 1) * 100


def prepare_panel(panel: dict[str, pd.DataFrame], specs: list[dict]) -> None:
    """Pre-compute cross-sectional features the specs need (mutates panel frames)."""
    exprs = [e for s in specs for e in R.all_exprs(s)]
    F.add_cross_sectional(panel, exprs)


def ticker_trades(
    code: str, df: pd.DataFrame, spec: dict, costs: dict, regimes: pd.Series | None,
    start: pd.Timestamp | None = None, end: pd.Timestamp | None = None, min_value: float = 0,
) -> list[Trade]:
    """Simulate one ticker sequentially (no overlapping positions in the same ticker)."""
    if len(df) < 60:
        return []
    mask = R.entry_mask(df, spec, min_value).to_numpy()
    if not mask.any():
        return []
    allowed = spec.get("regimes")
    reg = regimes.reindex(df.index).fillna("neutral").to_numpy() if regimes is not None else None
    score = R.rank_series(df, spec).to_numpy()
    exit_flag = R.exit_when_mask(df, spec)
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    atr = F.compute_feature(df, "atr_14").to_numpy()
    idx = df.index
    ex, ent = spec["exit"], spec["entry"]

    trades: list[Trade] = []
    i, n = 0, len(df)
    while i < n - 1:
        if not mask[i] or (start is not None and idx[i] < start) or (end is not None and idx[i] > end):
            i += 1
            continue
        rg = reg[i] if reg is not None else "neutral"
        if allowed and rg not in allowed:
            i += 1
            continue
        fill = R.entry_fill(o, h, i, h[i] - l[i], ent)
        if fill is None or fill <= 0:
            i += 1
            continue
        e_idx = i + 1
        stop, target = R.stop_target(fill, atr[i], ex)
        res = R.simulate_exit(o, h, l, c, atr, exit_flag, e_idx, fill, stop, target, ex,
                              intrabar_entry=ent.get("type") == "breakout")
        if res.idx is None:  # still open at data end → mark to last close
            x_idx, x_price, reason = n - 1, c[-1], "open"
        else:
            x_idx, x_price, reason = res.idx, res.price, res.reason
        trades.append(Trade(
            code, idx[i], idx[e_idx], idx[x_idx], fill, float(x_price), reason,
            net_return(fill, float(x_price), costs), float(np.nan_to_num(score[i], nan=-1e18)), str(rg),
            x_idx - e_idx + 1,
        ))
        i = x_idx + 1 if x_idx > i else i + 1
    return trades


def run(
    panel: dict[str, pd.DataFrame], spec: dict, costs: dict, regimes_by_code: dict[str, pd.Series],
    start: pd.Timestamp | None = None, end: pd.Timestamp | None = None, min_value: float = 0,
) -> list[Trade]:
    spec = R.resolve(spec)
    out: list[Trade] = []
    for code, df in panel.items():
        out += ticker_trades(code, df, spec, costs, regimes_by_code.get(code), start, end, min_value)
    return out


def portfolio(trades: list[Trade], max_positions: int) -> dict:
    """Equal-slot portfolio: each day, fill free slots with the highest-scored new trades.
    Equity is realised on exit (no intra-trade mark-to-market, so MDD is slightly understated)."""
    if not trades:
        return {"taken": [], "equity": pd.Series(dtype=float)}
    by_entry: dict[pd.Timestamp, list[Trade]] = {}
    for t in trades:
        by_entry.setdefault(t.entry_date, []).append(t)
    open_pos: list[tuple[Trade, float]] = []
    equity = 1.0
    cash_slots = max_positions
    curve: dict[pd.Timestamp, float] = {}
    taken: list[Trade] = []
    dates = sorted(set(by_entry) | {t.exit_date for t in trades})
    for d in dates:
        # exits first (positions exiting today free slots for tomorrow; same-day re-entry is fine)
        still = []
        for t, alloc in open_pos:
            if t.exit_date <= d:
                equity += alloc * t.ret_pct / 100
                cash_slots += 1
            else:
                still.append((t, alloc))
        open_pos = still
        for t in sorted(by_entry.get(d, []), key=lambda t: -t.score):
            if cash_slots <= 0:
                break
            if any(p.code == t.code for p, _ in open_pos):
                continue
            alloc = equity / max_positions
            open_pos.append((t, alloc))
            taken.append(t)
            cash_slots -= 1
        curve[d] = equity
    return {"taken": taken, "equity": pd.Series(curve).sort_index()}


def metrics(trades: list[Trade], equity: pd.Series | None = None) -> dict:
    if not trades:
        return {"trades": 0}
    r = np.array([t.ret_pct for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf")
    m = {
        "trades": int(len(r)),
        "win_rate": round(float((r > 0).mean() * 100), 1),
        "expectancy_pct": round(float(r.mean()), 3),
        "median_pct": round(float(np.median(r)), 3),
        "avg_win_pct": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss_pct": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "profit_factor": round(float(pf), 3) if math.isfinite(pf) else 99.0,
        "avg_hold": round(float(np.mean([t.hold for t in trades])), 1),
        "by_regime": {},
    }
    for rg in sorted({t.regime for t in trades}):
        rr = np.array([t.ret_pct for t in trades if t.regime == rg])
        m["by_regime"][rg] = {"trades": int(len(rr)), "expectancy_pct": round(float(rr.mean()), 3),
                              "win_rate": round(float((rr > 0).mean() * 100), 1)}
    if equity is not None and len(equity) > 1:
        peak = equity.cummax()
        mdd = float(((equity / peak) - 1).min() * 100)
        years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
        cagr = (equity.iloc[-1] ** (1 / years) - 1) * 100 if equity.iloc[-1] > 0 else -100.0
        m.update({"cagr_pct": round(float(cagr), 2), "mdd_pct": round(abs(mdd), 2),
                  "total_return_pct": round(float((equity.iloc[-1] - 1) * 100), 2)})
    return m


def evaluate_spec(
    panel, spec: dict, costs: dict, regimes_by_code, max_positions: int, oos_fraction: float,
    min_value: float = 0,
) -> dict:
    """Full-period + in-sample / out-of-sample metrics for a spec."""
    spec = R.validate(spec)
    trades = run(panel, spec, costs, regimes_by_code, min_value=min_value)
    if not trades:
        return {"full": {"trades": 0}, "is": {"trades": 0}, "oos": {"trades": 0}}
    first = min(df.index[0] for df in panel.values() if len(df))
    last = max(df.index[-1] for df in panel.values() if len(df))
    split = first + (last - first) * (1 - oos_fraction)
    pf_full = portfolio(trades, max_positions)
    is_tr = [t for t in trades if t.signal_date < split]
    oos_tr = [t for t in trades if t.signal_date >= split]
    pf_is, pf_oos = portfolio(is_tr, max_positions), portfolio(oos_tr, max_positions)
    return {
        "split": split.strftime("%Y-%m-%d"),
        "full": metrics(pf_full["taken"], pf_full["equity"]),
        "is": metrics(pf_is["taken"], pf_is["equity"]),
        "oos": metrics(pf_oos["taken"], pf_oos["equity"]),
        "raw_signals": len(trades),
    }


def passes_gate(result: dict, gate: dict) -> tuple[bool, list[str]]:
    full, oos = result.get("full", {}), result.get("oos", {})
    why = []
    if full.get("trades", 0) < gate["min_trades"]:
        why.append(f"거래 수 {full.get('trades', 0)} < {gate['min_trades']}")
    if full.get("profit_factor", 0) < gate["min_profit_factor"]:
        why.append(f"PF {full.get('profit_factor')} < {gate['min_profit_factor']}")
    if full.get("expectancy_pct", -1) < gate["min_expectancy_pct"]:
        why.append(f"기대값 {full.get('expectancy_pct')}% < {gate['min_expectancy_pct']}%")
    if oos.get("trades", 0) < 5 or oos.get("profit_factor", 0) < gate["min_oos_profit_factor"]:
        why.append(f"OOS PF {oos.get('profit_factor')} (거래 {oos.get('trades', 0)}) 미달")
    if full.get("mdd_pct", 0) > gate["max_mdd_pct"]:
        why.append(f"MDD {full.get('mdd_pct')}% > {gate['max_mdd_pct']}%")
    return (not why), why


def objective(m: dict) -> float:
    """Score used to rank parameter sets in-sample: expectancy × √trades, penalised by drawdown."""
    if m.get("trades", 0) < 20:
        return -1e9
    return m["expectancy_pct"] * math.sqrt(m["trades"]) - 0.05 * m.get("mdd_pct", 0)


def walk_forward_tune(panel, spec: dict, costs, regimes_by_code, max_positions, oos_fraction, max_combos=24,
                      min_value: float = 0) -> dict | None:
    """Pick params on the in-sample window, accept only if they also beat current params out-of-sample."""
    grid = spec.get("param_grid") or {}
    if not grid:
        return None
    keys = list(grid)
    combos = list(itertools.product(*(grid[k] for k in keys)))[:max_combos]
    base = evaluate_spec(panel, spec, costs, regimes_by_code, max_positions, oos_fraction, min_value)
    best, best_score = None, objective(base["is"])
    for combo in combos:
        params = dict(zip(keys, combo))
        if all(spec["params"].get(k) == v for k, v in params.items()):
            continue
        cand = dict(spec, params={**spec["params"], **params})
        res = evaluate_spec(panel, cand, costs, regimes_by_code, max_positions, oos_fraction, min_value)
        sc = objective(res["is"])
        if sc > best_score:
            best, best_score = (params, res), sc
    if not best:
        return None
    params, res = best
    oos_new, oos_old = res["oos"], base["oos"]
    improved = (
        oos_new.get("trades", 0) >= 10
        and oos_new.get("expectancy_pct", -9) > oos_old.get("expectancy_pct", -9)
        and oos_new.get("profit_factor", 0) >= 1.0
    )
    return {"params": params, "result": res, "baseline": base, "improved": improved}


def trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    return pd.DataFrame([asdict(t) for t in trades])

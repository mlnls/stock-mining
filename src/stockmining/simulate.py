"""Walk-forward mock trading of the whole system + Monte Carlo odds.

Honest mock trading: at the start of each quarter, strategies are chosen using ONLY trades that had
already closed before that day; then the quarter is traded with those choices. Repeat. This is how the
learner would actually have performed, not how the best-in-hindsight strategy looks.

Policies compared
  all_equal      every strategy, no selection
  learner        strategies whose trailing (shrunk) expectancy in the trade's market regime is > 0,
                 candidates ranked by that expectancy
  best_trailing  only the single best strategy by trailing expectancy each quarter
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import backtest as BT
from . import rules as R


@dataclass
class PolicyResult:
    name: str
    max_positions: int
    metrics: dict
    equity: pd.Series
    odds: dict


def all_trades(app, ctx) -> dict[str, list[BT.Trade]]:
    """Every strategy's trades, with ``score`` replaced by its percentile among that strategy's
    signals on the same day (raw rank values have different scales across strategies)."""
    panel, regs, _, _ = ctx
    out = {}
    for st in app.db.strategies(("active", "muted")):
        spec = R.resolve(st["spec"])
        BT.prepare_panel(panel, [spec])
        trades = BT.run(panel, spec, app.cfg["costs"], regs, min_value=app.min_value)
        if trades:
            df = pd.DataFrame({"d": [t.signal_date for t in trades], "s": [t.score for t in trades]})
            pct = df.groupby("d")["s"].rank(pct=True).to_numpy()
            trades = [BT.Trade(**{**t.__dict__, "score": float(p)}) for t, p in zip(trades, pct)]
        out[st["name"]] = trades
    return out


def _trailing_expectancy(trades: list[BT.Trade], asof: pd.Timestamp, window_days: int, prior_n: float = 30):
    """Per (regime) expectancy from trades closed before ``asof``, shrunk toward the strategy's overall mean."""
    past = [t for t in trades if t.exit_date < asof and t.signal_date >= asof - pd.Timedelta(days=window_days)]
    if len(past) < 10:
        return None
    r = np.array([t.ret_pct for t in past])
    overall = r.mean()
    by = {}
    for rg in ("bull", "neutral", "bear"):
        rr = np.array([t.ret_pct for t in past if t.regime == rg])
        k = prior_n / 3
        by[rg] = (k * overall + rr.sum()) / (k + len(rr))  # shrink sparse regimes toward overall
    return {"overall": overall, "by_regime": by, "n": len(past)}


def walk_forward(trades_by_strat: dict[str, list[BT.Trade]], start: pd.Timestamp, end: pd.Timestamp,
                 policy: str, window_days: int = 730, step: str = "QS") -> list[BT.Trade]:
    chosen: list[BT.Trade] = []
    edges = list(pd.date_range(start, end, freq=step)) + [end]
    for qs, qe in zip(edges[:-1], edges[1:]):
        stats = {n: _trailing_expectancy(tr, qs, window_days) for n, tr in trades_by_strat.items()}
        stats = {n: s for n, s in stats.items() if s}
        if policy == "best_trailing" and stats:
            best = max(stats, key=lambda n: stats[n]["overall"])
            if stats[best]["overall"] <= 0:
                continue
        for name, trades in trades_by_strat.items():
            s = stats.get(name)
            for t in trades:
                if not (qs <= t.signal_date < qe):
                    continue
                if policy == "all_equal":
                    chosen.append(t)
                elif policy == "learner" and s and s["by_regime"][t.regime] > 0:
                    # same scoring as the live engine: strategy weight × (0.5 + 0.5 × signal percentile)
                    w = s["by_regime"][t.regime]
                    chosen.append(BT.Trade(**{**t.__dict__, "score": w * (0.5 + 0.5 * t.score)}))
                elif policy == "best_trailing" and stats and name == best:
                    chosen.append(t)
    return chosen


def monthly_returns(equity: pd.Series) -> pd.Series:
    if len(equity) < 2:
        return pd.Series(dtype=float)
    m = equity.resample("ME").last().ffill()
    return m.pct_change().dropna()


def monte_carlo(monthly: pd.Series, months: int = 12, n: int = 20000, seed: int = 0) -> dict:
    """Bootstrap months (with replacement) → distribution of 1-year outcomes."""
    if len(monthly) < 6:
        return {}
    rng = np.random.default_rng(seed)
    samples = rng.choice(monthly.to_numpy(), size=(n, months), replace=True)
    paths = np.cumprod(1 + samples, axis=1)
    final = paths[:, -1] - 1
    peaks = np.maximum.accumulate(np.concatenate([np.ones((n, 1)), paths], axis=1), axis=1)[:, 1:]
    dd = (paths / peaks - 1).min(axis=1)
    return {
        "p_profit_1y": round(float((final > 0).mean() * 100), 1),
        "p_gt_10pct": round(float((final > 0.10).mean() * 100), 1),
        "p_gt_20pct": round(float((final > 0.20).mean() * 100), 1),
        "p_loss_gt_10pct": round(float((final < -0.10).mean() * 100), 1),
        "p_drawdown_gt_20pct": round(float((dd < -0.20).mean() * 100), 1),
        "median_1y_pct": round(float(np.median(final) * 100), 1),
        "p5_1y_pct": round(float(np.percentile(final, 5) * 100), 1),
        "p95_1y_pct": round(float(np.percentile(final, 95) * 100), 1),
        "pos_month_pct": round(float((monthly > 0).mean() * 100), 1),
    }


def _bounds(ctx, warmup_days=730):
    panel = ctx[0]
    first = min(df.index[0] for df in panel.values() if len(df))
    last = max(df.index[-1] for df in panel.values() if len(df))
    return (first + pd.Timedelta(days=200 + warmup_days)).to_period("Q").to_timestamp(), last


def project(monthly: pd.Series, start_amount: float, years=(1, 3), haircut: float = 0.5, n: int = 20000,
            seed: int = 0) -> dict:
    """Bootstrap the walk-forward monthly returns; ``haircut`` removes that share of the mean monthly
    return (survivorship bias, data snooping, real fills) before projecting."""
    m = monthly.to_numpy()
    if len(m) < 6:
        return {}
    m = m - m.mean() * haircut
    rng = np.random.default_rng(seed)
    out = {}
    for y in years:
        paths = np.cumprod(1 + rng.choice(m, size=(n, 12 * y), replace=True), axis=1)
        peaks = np.maximum.accumulate(np.concatenate([np.ones((n, 1)), paths], axis=1), axis=1)[:, 1:]
        f = paths[:, -1] * start_amount
        out[y] = {"p5": float(np.percentile(f, 5)), "p25": float(np.percentile(f, 25)),
                  "median": float(np.median(f)), "p75": float(np.percentile(f, 75)),
                  "p95": float(np.percentile(f, 95)), "p_loss": float((f < start_amount).mean() * 100),
                  "p_half": float((f < start_amount / 2).mean() * 100),
                  "p_dd30": float(((paths / peaks - 1).min(axis=1) < -0.30).mean() * 100)}
    return out


def compare_profiles(app, ctx=None) -> list[dict]:
    """안정형 / 기본형 / 공격형 side by side, same walk-forward window."""
    from .config import PROFILES
    ctx = ctx or app.full_context()
    tbs = all_trades(app, ctx)
    start, last = _bounds(ctx)
    aggr = PROFILES["aggressive"]
    setups = [
        ("안정형: 전체 전략 균등 · 10종목", "all_equal", None, 10),
        ("기본형: 학습기 · 5종목", "learner", None, 5),
        ("공격형: 추세·돌파 4개 균등 · 3종목", "all_equal", aggr["strategy_include"], aggr["account"]["max_positions"]),
    ]
    out = []
    for label, policy, include, mp in setups:
        pool = {k: v for k, v in tbs.items() if not include or k in include}
        pf = BT.portfolio(walk_forward(pool, start, last, policy), mp)
        monthly = monthly_returns(pf["equity"])
        out.append({"label": label, "metrics": BT.metrics(pf["taken"], pf["equity"]),
                    "odds": monte_carlo(monthly), "monthly": monthly,
                    "worst_month_pct": round(float(monthly.min() * 100), 1) if len(monthly) else None})
    return out


def run(app, ctx=None, warmup_days: int = 730, positions=(5, 10)) -> list[PolicyResult]:
    ctx = ctx or app.full_context()
    tbs = all_trades(app, ctx)
    panel = ctx[0]
    first = min(df.index[0] for df in panel.values() if len(df))
    last = max(df.index[-1] for df in panel.values() if len(df))
    start = (first + pd.Timedelta(days=200 + warmup_days)).to_period("Q").to_timestamp()
    results = []
    for policy in ("all_equal", "learner", "best_trailing"):
        trades = walk_forward(tbs, start, last, policy)
        for mp in positions:
            pf = BT.portfolio(trades, mp)
            eq = pf["equity"]
            results.append(PolicyResult(policy, mp, BT.metrics(pf["taken"], eq), eq,
                                        monte_carlo(monthly_returns(eq))))
    return results


def luck_check(app, ctx=None, runs: int = 200, max_positions: int = 5, seed: int = 1) -> dict:
    """Pick among ALL strategies' signals at random (random scores) many times → spread of outcomes.
    If a policy can't beat the median random pick, its 'skill' is indistinguishable from luck."""
    ctx = ctx or app.full_context()
    tbs = all_trades(app, ctx)
    panel = ctx[0]
    first = min(df.index[0] for df in panel.values() if len(df))
    last = max(df.index[-1] for df in panel.values() if len(df))
    start = (first + pd.Timedelta(days=930)).to_period("Q").to_timestamp()
    base = [t for tr in tbs.values() for t in tr if start <= t.signal_date < last]
    rng = np.random.default_rng(seed)
    cagr = []
    for _ in range(runs):
        shuffled = [BT.Trade(**{**t.__dict__, "score": float(x)}) for t, x in zip(base, rng.random(len(base)))]
        pf = BT.portfolio(shuffled, max_positions)
        cagr.append(BT.metrics(pf["taken"], pf["equity"]).get("cagr_pct", 0))
    c = np.array(cagr)
    return {"runs": runs, "median_cagr": round(float(np.median(c)), 1), "p5": round(float(np.percentile(c, 5)), 1),
            "p95": round(float(np.percentile(c, 95)), 1), "p_positive": round(float((c > 0).mean() * 100), 1)}

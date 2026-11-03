"""Self-learning: combine backtest priors with live paper-trade evidence to weight strategies by regime.

Every strategy (even muted ones) keeps paper trading, so a muted strategy can earn its way back.
Posterior expectancy (Bayesian shrinkage of the paper-trade mean toward the backtest prior)::

    post = (prior_n * prior_exp + Σ paper_ret) / (prior_n + n_paper)
"""
from __future__ import annotations

import json
from datetime import datetime

import numpy as np

from .db import DB


def strategy_stats(db: DB, strategies: list[dict], regime: str, cfg: dict) -> list[dict]:
    lc = cfg["learner"]
    out = []
    for st in strategies:
        bt = st.get("backtest") or {}
        full = bt.get("full", {})
        prior = (full.get("by_regime") or {}).get(regime) or full
        prior_exp = float(prior.get("expectancy_pct", 0.0) or 0.0)
        prior_n = lc["prior_trades"] if prior.get("trades", 0) >= 20 else lc["prior_trades"] / 3
        rows = db.query("SELECT ret_pct, regime FROM paper_trades WHERE strategy=? AND status='closed'", (st["name"],))
        rets_reg = np.array([r["ret_pct"] for r in rows if r["regime"] == regime])
        rets = rets_reg if len(rets_reg) >= 5 else np.array([r["ret_pct"] for r in rows])
        n = len(rets)
        post = (prior_n * prior_exp + rets.sum()) / (prior_n + n) if (prior_n + n) else 0.0
        wins, losses = rets[rets > 0], rets[rets <= 0]
        out.append({
            "name": st["name"],
            "status": st["status"],
            "regimes": st["spec"].get("regimes"),
            "prior_expectancy_pct": round(prior_exp, 3),
            "paper_trades": int(n),
            "paper_expectancy_pct": round(float(rets.mean()), 3) if n else None,
            "paper_win_rate": round(float((rets > 0).mean() * 100), 1) if n else None,
            "paper_profit_factor": round(float(wins.sum() / -losses.sum()), 2) if n and losses.sum() < 0 else None,
            "posterior_expectancy_pct": round(float(post), 3),
            "regime_allowed": not st["spec"].get("regimes") or regime in st["spec"]["regimes"],
        })
    return out


def weights_from_stats(stats: list[dict], selection: str = "learner", include: list[str] | None = None) -> dict[str, float]:
    """learner: ∝ positive posterior expectancy · equal: same weight for every eligible strategy.
    Eligible = active, allowed in the current regime, in ``include`` (if given), posterior expectancy > 0."""
    eligible = [s for s in stats if s["status"] == "active" and s["regime_allowed"]
                and (not include or s["name"] in include) and s["posterior_expectancy_pct"] > 0]
    if selection == "equal":
        raw = {s["name"]: 1.0 for s in eligible}
    else:
        raw = {s["name"]: s["posterior_expectancy_pct"] for s in eligible}
    tot = sum(raw.values())
    return {k: (v / tot if tot else 0.0) for k, v in raw.items()}


def update_statuses(db: DB, stats: list[dict], cfg: dict) -> list[str]:
    """Mute strategies whose live evidence is negative; un-mute when they recover. Returns change log."""
    lc = cfg["learner"]
    now = datetime.now().isoformat(timespec="seconds")
    changes = []
    for s in stats:
        if s["status"] == "active" and s["paper_trades"] >= lc["min_paper_trades_to_mute"] \
                and (s["paper_expectancy_pct"] or 0) < 0 and s["posterior_expectancy_pct"] < 0:
            db.execute("UPDATE strategies SET status='muted', updated=? WHERE name=?", (now, s["name"]))
            changes.append(f"{s['name']} 음소거(페이퍼 기대값 {s['paper_expectancy_pct']}%, {s['paper_trades']}건)")
        elif s["status"] == "muted" and s["posterior_expectancy_pct"] > 0.1 and (s["paper_expectancy_pct"] or 0) > 0:
            db.execute("UPDATE strategies SET status='active', updated=? WHERE name=?", (now, s["name"]))
            changes.append(f"{s['name']} 재활성화(사후 기대값 {s['posterior_expectancy_pct']}%)")
    if changes:
        db.set("learner_changes_" + now[:10], json.dumps(changes, ensure_ascii=False))
    return changes

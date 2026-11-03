"""Daily strategy engine: signals → paper trades (the system's own learning loop) → recommendations."""
from __future__ import annotations

import logging
import math
from datetime import timedelta

import numpy as np
import pandas as pd

from . import backtest as BT
from . import features as F
from . import regime as RG
from . import rules as R
from .db import DB

log = logging.getLogger(__name__)


def load_context(db: DB, cfg: dict, lookback_days: int = 800, codes: list[str] | None = None):
    """Panel + per-code regime series + index frames."""
    last = db.last_price_date()
    start = (pd.Timestamp(last) - timedelta(days=lookback_days)).strftime("%Y-%m-%d") if last else None
    codes = codes if codes is not None else db.universe()
    panel = db.load_panel(codes, start)
    info = db.ticker_info()
    from .data import indices
    idx = {c: db.load_index(c) for c in indices(cfg)}
    reg = {c: RG.regime_series(df) for c, df in idx.items()}
    regimes_by_code = {code: reg.get(RG.index_code(info.get(code, {}).get("market"), cfg), pd.Series(dtype=object))
                       for code in panel}
    return panel, regimes_by_code, idx, info


# ---------------------------------------------------------------------------- signals
def generate_signals(db: DB, panel, regimes_by_code, strategies: list[dict], date: str) -> pd.DataFrame:
    ts = pd.Timestamp(date)
    specs = [R.resolve(s["spec"]) for s in strategies]
    BT.prepare_panel(panel, specs)
    rows = []
    for st, spec in zip(strategies, specs):
        allowed = spec.get("regimes")
        for code, df in panel.items():
            if len(df) < 60 or df.index[-1] != ts:
                continue
            reg = regimes_by_code.get(code)
            rg = reg.get(ts, "neutral") if reg is not None and len(reg) else "neutral"
            if allowed and rg not in allowed:
                continue
            try:
                if not bool(R.entry_mask(df, spec).iloc[-1]):
                    continue
            except F.ExprError as e:
                log.error("strategy %s broken: %s", st["name"], e)
                break
            close = float(df["close"].iloc[-1])
            atr = float(F.compute_feature(df, "atr_14").iloc[-1])
            stop, target = R.stop_target(close, atr, spec["exit"])
            score = float(np.nan_to_num(R.rank_series(df, spec).iloc[-1], nan=0.0))
            rows.append({"date": date, "strategy": st["name"], "code": code, "score": score, "close": close,
                         "atr": atr, "stop": stop, "target": target, "regime": rg})
    out = pd.DataFrame(rows)
    db.execute("DELETE FROM signals WHERE date=?", (date,))
    if not out.empty:
        db.executemany(
            "INSERT INTO signals(date,strategy,code,score,close,atr,stop,target) VALUES(?,?,?,?,?,?,?,?)",
            out[["date", "strategy", "code", "score", "close", "atr", "stop", "target"]].itertuples(index=False),
        )
    return out


# ---------------------------------------------------------------------------- paper trading
def open_paper_trades(db: DB, signals: pd.DataFrame, top_k: int) -> int:
    if signals.empty:
        return 0
    n = 0
    for strat, g in signals.groupby("strategy"):
        for r in g.sort_values("score", ascending=False).head(top_k).itertuples():
            cur = db.execute(
                """INSERT OR IGNORE INTO paper_trades(strategy,code,signal_date,regime,atr,status)
                   VALUES(?,?,?,?,?,'pending')""", (strat, r.code, r.date, r.regime, r.atr))
            n += cur.rowcount
    return n


def update_paper_trades(db: DB, panel: dict[str, pd.DataFrame], strategies: dict[str, dict], costs: dict) -> dict:
    """(Re)simulate every pending/open paper trade over the data available so far."""
    rows = db.query("SELECT * FROM paper_trades WHERE status IN ('pending','open')")
    stats = {"filled": 0, "closed": 0, "expired": 0}
    for t in rows:
        st = strategies.get(t["strategy"])
        df = panel.get(t["code"])
        if st is None or df is None:
            continue
        spec = R.resolve(st["spec"])
        ts = pd.Timestamp(t["signal_date"])
        if ts not in df.index:
            continue
        i = df.index.get_loc(ts)
        if i + 1 >= len(df):
            continue  # next session not in data yet
        o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
        fill = R.entry_fill(o, h, i, h[i] - l[i], spec["entry"])
        if fill is None:
            db.execute("UPDATE paper_trades SET status='expired' WHERE id=?", (t["id"],))
            stats["expired"] += 1
            continue
        atr_sig = t["atr"] or float(F.compute_feature(df, "atr_14").iloc[i])
        stop, target = R.stop_target(fill, atr_sig, spec["exit"])
        atr = F.compute_feature(df, "atr_14").to_numpy()
        res = R.simulate_exit(o, h, l, c, atr, R.exit_when_mask(df, spec), i + 1, fill, stop, target, spec["exit"],
                              intrabar_entry=spec["entry"].get("type") == "breakout")
        entry_date = df.index[i + 1].strftime("%Y-%m-%d")
        if t["status"] == "pending":
            stats["filled"] += 1
        if res.idx is None:
            db.execute("UPDATE paper_trades SET status='open', entry_date=?, entry_price=?, stop=?, target=? WHERE id=?",
                       (entry_date, fill, res.stop, target, t["id"]))
        else:
            ret = BT.net_return(fill, float(res.price), costs)
            db.execute(
                """UPDATE paper_trades SET status='closed', entry_date=?, entry_price=?, stop=?, target=?,
                   exit_date=?, exit_price=?, exit_reason=?, ret_pct=? WHERE id=?""",
                (entry_date, fill, res.stop, target, df.index[res.idx].strftime("%Y-%m-%d"), float(res.price),
                 res.reason, ret, t["id"]))
            stats["closed"] += 1
    return stats


# ---------------------------------------------------------------------------- recommendations
def recommend(db: DB, signals: pd.DataFrame, weights: dict[str, float], cfg: dict, held: set[str], date: str,
              blackout=None) -> pd.DataFrame:
    """``blackout(code) -> (bool, next_earnings)``: candidates with earnings too close are skipped and the
    next-best candidate takes the slot. Skipped names are kept in ``picks.attrs['skipped']``."""
    acct = cfg["account"]
    db.execute("DELETE FROM recommendations WHERE date=?", (date,))
    if signals.empty:
        return pd.DataFrame()
    s = signals[signals["strategy"].map(lambda x: weights.get(x, 0) > 0)].copy()
    s = s[~s["code"].isin(held)]
    # too-volatile names: an ATR stop more than max_stop_pct below the close is not a swing trade
    s = s[(s["close"] - s["stop"]) / s["close"] * 100 <= acct.get("max_stop_pct", 15)]
    if s.empty:
        return pd.DataFrame()
    s["pct"] = s.groupby("strategy")["score"].rank(pct=True)
    s["w"] = s["strategy"].map(weights) * (0.5 + 0.5 * s["pct"])
    agg = (s.sort_values("w", ascending=False)
             .groupby("code")
             .agg(combined_score=("w", "sum"), strategy=("strategy", lambda x: ",".join(x)),
                  close=("close", "first"), stop=("stop", "first"), target=("target", "first"))
             .sort_values("combined_score", ascending=False))
    slots = max(0, min(acct["max_new_per_day"], acct["max_positions"] - len(held)))
    chosen, skipped, earn = [], [], {}
    for code in agg.index:
        if len(chosen) >= slots:
            break
        if blackout:
            hit, nxt = blackout(code)
            earn[code] = nxt
            if hit:
                skipped.append((code, nxt))
                continue
        chosen.append(code)
    picks = agg.loc[chosen].reset_index()
    picks["next_earnings"] = [earn.get(c) for c in picks["code"]]
    risk_amt = acct["capital"] * acct["risk_per_trade"]
    shares = []
    for r in picks.itertuples():
        per_share = max(r.close - r.stop, r.close * 0.01)
        n = min(risk_amt / per_share, acct["capital"] * acct["max_position_weight"] / r.close)
        n = math.floor(n * 10_000) / 10_000 if acct.get("fractional_shares") else math.floor(n)
        shares.append(max(n, 0))
    picks["shares"] = shares
    db.executemany(
        """INSERT INTO recommendations(date,code,strategy,combined_score,entry_ref,stop,target,shares)
           VALUES(?,?,?,?,?,?,?,?)""",
        [(date, r.code, r.strategy, float(r.combined_score), float(r.close), float(r.stop),
          None if pd.isna(r.target) else float(r.target), r.shares) for r in picks.itertuples()],
    )
    picks.attrs["skipped"] = skipped
    return picks

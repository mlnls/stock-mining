"""Machine-learning score ``ml_score``: gradient boosting on daily price features.

Target: how a stock's next-10-day return (next open → close 10 days later) ranks against all other stocks
that day. Output: the model's prediction turned into a daily percentile 0–100 (higher = expected to beat
more of the market), usable in rule specs like any feature (``ml_score >= 98``).

No look-ahead: walk-forward. Every RETRAIN trading days a fresh model is fit only on rows whose label
was fully known GAP days before the block starts, then predicts that block. Rows before the first block
(less than MIN_TRAIN days of history) get NaN, so no signals there.
"""
from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

from . import features as F

log = logging.getLogger(__name__)

HORIZON = 10          # label: next open → close HORIZON days later
GAP = HORIZON + 2     # training rows must end this many days before a block starts
RETRAIN = 63          # ~ quarterly
MIN_TRAIN = 252       # ~ 1 year of history before the first prediction
MAX_ROWS = 300_000    # subsample training rows (memory / time on a small server)
LIQUID_PCT = 30       # train only on the more liquid 70% of each day
MIN_ROWS = 20_000     # skip a block if there is too little training data

_RAW = ["ret_5", "ret_20", "ret_60", "ret_120", "natr_14", "rsi_2", "rsi_14", "bbw_20", "vol_ratio_20",
        "up_days_10", "gap"]
_XRANK = ["ret_20", "ret_60", "natr_14", "vavg_20"]
FEATURE_NAMES = (_RAW + ["d_sma20", "d_sma50", "d_sma200", "d_hh120", "slope_sma50", "log_value"]
                 + [f"x_{n}" for n in _XRANK])


_BARS = ["open", "high", "low", "close", "volume", "value"]


def _ticker_frame(df: pd.DataFrame) -> pd.DataFrame:
    # work on a bare copy so the ~20 helper columns are not cached on the shared panel frames (memory)
    df = df[_BARS].copy()
    c = df["close"]
    cols = {n: F.compute_feature(df, n) for n in _RAW}
    cols["d_sma20"] = c / F.compute_feature(df, "sma_20") - 1
    cols["d_sma50"] = c / F.compute_feature(df, "sma_50") - 1
    cols["d_sma200"] = c / F.compute_feature(df, "sma_200") - 1
    cols["d_hh120"] = c / F.compute_feature(df, "hh_120") - 1
    s50 = F.compute_feature(df, "sma_50")
    cols["slope_sma50"] = s50 / s50.shift(10) - 1
    cols["log_value"] = np.log10(F.compute_feature(df, "vavg_20").clip(lower=1))
    cols["vavg_20"] = F.compute_feature(df, "vavg_20")
    cols["label"] = c.shift(-HORIZON) / df["open"].shift(-1) - 1
    out = pd.DataFrame(cols, index=df.index).astype("float32")
    return out.replace([np.inf, -np.inf], np.nan)


def build_frame(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    parts = []
    for code, df in panel.items():
        if len(df) < 60:
            continue
        f = _ticker_frame(df)
        f["code"] = code
        parts.append(f)
    if not parts:
        return pd.DataFrame()
    long = pd.concat(parts, copy=False)
    del parts
    long.index.name = "date"
    long = long.reset_index()
    long["code"] = long["code"].astype("category")
    g = long.groupby("date")
    for n in _XRANK:
        long[f"x_{n}"] = g[n].rank(pct=True).astype("float32") * 100
    long["y"] = g["label"].rank(pct=True).astype("float32")
    return long.drop(columns=["label", "vavg_20"])


def _model():
    from sklearn.ensemble import HistGradientBoostingRegressor
    return HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=300,
                                         l2_regularization=1.0, random_state=0)


def walk_forward_scores(long: pd.DataFrame) -> pd.Series:
    """Out-of-sample prediction per row (NaN where no model was available yet)."""
    dates = np.sort(long["date"].unique())
    di = pd.Series(np.arange(len(dates)), index=dates)
    long_di = di.reindex(long["date"]).to_numpy()
    liquid = long["x_vavg_20"].to_numpy() >= LIQUID_PCT
    X = long[FEATURE_NAMES].to_numpy(dtype=np.float32)
    long.drop(columns=FEATURE_NAMES, inplace=True)  # X holds them now
    y = long["y"].to_numpy()
    pred = np.full(len(long), np.nan, dtype=np.float32)
    rng = np.random.default_rng(0)
    for start in range(MIN_TRAIN, len(dates), RETRAIN):
        t0 = time.time()
        tr = np.flatnonzero((long_di < start - GAP) & liquid & ~np.isnan(y))
        if len(tr) < MIN_ROWS:
            continue
        if len(tr) > MAX_ROWS:
            tr = rng.choice(tr, MAX_ROWS, replace=False)
        Xtr = X[tr]
        keep = np.flatnonzero((~np.isnan(Xtr)).mean(axis=0) > 0.5)  # skip features not yet computable
        m = _model().fit(Xtr[:, keep], y[tr])
        te = np.flatnonzero((long_di >= start) & (long_di < start + RETRAIN))
        pred[te] = m.predict(X[te][:, keep])
        log.info("ml block %s..%s: train=%s rows, %.1fs", str(dates[start])[:10],
                 str(dates[min(start + RETRAIN, len(dates)) - 1])[:10], len(tr), time.time() - t0)
    return pd.Series(pred, index=long.index)


def add_ml_score(panel: dict[str, pd.DataFrame]) -> None:
    """Set ``df['ml_score']`` (daily percentile 0–100 of the walk-forward prediction) on every frame."""
    if not panel or all("ml_score" in df.columns for df in panel.values()):
        return
    t0 = time.time()
    long = build_frame(panel)
    if long.empty:
        for df in panel.values():
            df["ml_score"] = np.nan
        return
    p = walk_forward_scores(long)
    out = pd.DataFrame({"date": long["date"], "code": long["code"].astype(str), "p": p})
    del long
    out["ml_score"] = (out.groupby("date")["p"].rank(pct=True) * 100).astype("float32")
    wide = out.pivot(index="date", columns="code", values="ml_score")
    del out
    for code, df in panel.items():
        df["ml_score"] = wide[code].reindex(df.index) if code in wide.columns else np.nan
    log.info("ml_score for %s tickers in %.0fs", len(panel), time.time() - t0)

"""Market regime from index trend: bull / neutral / bear."""
from __future__ import annotations

import pandas as pd

INDEX_FOR_MARKET = {"KOSPI": "KS11", "KOSDAQ": "KQ11", "KOSDAQ GLOBAL": "KQ11"}


def regime_series(index_df: pd.DataFrame) -> pd.Series:
    """bull: close > SMA50 > SMA200 · bear: close < SMA200 and SMA50 < SMA200 · else neutral."""
    if index_df is None or index_df.empty:
        return pd.Series(dtype=object)
    c = index_df["close"]
    s50 = c.rolling(50, min_periods=50).mean()
    s200 = c.rolling(200, min_periods=200).mean()
    out = pd.Series("neutral", index=index_df.index, dtype=object)
    out[(c > s50) & (s50 > s200)] = "bull"
    out[(c < s200) & (s50 < s200)] = "bear"
    out[s200.isna()] = "neutral"
    return out


def index_code(market: str | None, cfg: dict) -> str:
    """Index that defines the regime for a ticker: US → S&P 500 for all; KR → KOSPI / KOSDAQ by listing."""
    if cfg["market"] == "us":
        return cfg["regime_index"]
    return INDEX_FOR_MARKET.get((market or "").upper(), cfg["regime_index"])


def describe(index_df: pd.DataFrame) -> dict:
    if index_df is None or index_df.empty:
        return {}
    c = index_df["close"]
    reg = regime_series(index_df)
    return {
        "date": index_df.index[-1].strftime("%Y-%m-%d"),
        "close": round(float(c.iloc[-1]), 2),
        "chg_1d_pct": round(float(c.pct_change().iloc[-1] * 100), 2),
        "chg_20d_pct": round(float((c.iloc[-1] / c.iloc[-21] - 1) * 100), 2) if len(c) > 21 else None,
        "regime": reg.iloc[-1],
    }

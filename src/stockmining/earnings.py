"""Next earnings date per ticker (US, free Yahoo data via yfinance), cached daily in the kv table.

Used to enforce the principle "don't open a swing trade within N trading days of earnings" without
needing Claude's web search.
"""
from __future__ import annotations

import json
import logging
import warnings

import pandas as pd

from .db import DB

log = logging.getLogger(__name__)


def next_earnings(db: DB, code: str, today: str) -> str | None:
    """YYYY-MM-DD of the next earnings date on/after ``today``, or None if unknown."""
    key = f"earn:{code}"
    cached = db.get(key)
    if cached:
        c = json.loads(cached)
        if c.get("checked") == today:
            return c.get("next")
    nxt = None
    try:
        import yfinance as yf
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = yf.Ticker(code).get_earnings_dates(limit=8)
        if df is not None and len(df):
            dates = sorted({pd.Timestamp(d).tz_localize(None).normalize() for d in df.index})
            future = [d for d in dates if d >= pd.Timestamp(today)]
            nxt = future[0].strftime("%Y-%m-%d") if future else None
    except Exception as e:  # network / unknown symbol → treat as unknown, never block on it
        log.warning("earnings lookup %s failed: %s", code, e)
    db.set(key, json.dumps({"checked": today, "next": nxt}))
    return nxt


def trading_days_until(today: str, day: str) -> int:
    """Business days from ``today`` (exclusive) to ``day`` (inclusive); 0 if same day."""
    return max(len(pd.bdate_range(pd.Timestamp(today) + pd.Timedelta(days=1), day)), 0)


def in_blackout(db: DB, code: str, today: str, days: int) -> tuple[bool, str | None]:
    nxt = next_earnings(db, code, today)
    if not nxt:
        return False, None
    return trading_days_until(today, nxt) <= days, nxt

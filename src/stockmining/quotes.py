"""Live quotes for the dashboard: KRX via Naver's real-time polling API, US / FX / indices via yfinance.

Both are unofficial, free endpoints meant for light personal use, so results are cached briefly and the
dashboard asks for at most a few dozen symbols at a time. Every function degrades to ``None`` on failure.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

TTL = 20                      # seconds
_cache: dict[str, tuple[float, dict | None]] = {}
_lock = threading.Lock()
NAVER = "https://polling.finance.naver.com/api/realtime/domestic/stock/{codes}"
INDICES = {"^GSPC": "S&P500", "^IXIC": "나스닥", "KRW=X": "원/달러"}


def is_kr(code: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", code))


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _naver(codes: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(codes), 20):
        chunk = codes[i:i + 20]
        try:
            r = requests.get(NAVER.format(codes=",".join(chunk)), timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            for d in r.json().get("datas", []):
                price = _num(d.get("closePriceRaw"))
                chg = _num(d.get("compareToPreviousClosePriceRaw"))
                out[d["itemCode"]] = {
                    "price": price, "change": chg, "change_pct": _num(d.get("fluctuationsRatioRaw")),
                    "prev": price - chg if price is not None and chg is not None else None,
                    "name": d.get("stockName"), "currency": "KRW",
                    "open": d.get("marketStatus") == "OPEN", "time": d.get("localTradedAt"),
                }
        except Exception as e:
            log.warning("naver quote failed for %s: %s", chunk, e)
    return out


def us_market_open(now: datetime | None = None) -> bool:
    ny = (now or datetime.now(ZoneInfo("America/New_York"))).astimezone(ZoneInfo("America/New_York"))
    return ny.weekday() < 5 and (9, 30) <= (ny.hour, ny.minute) < (16, 0)


def _yahoo_one(code: str) -> dict | None:
    import yfinance as yf
    try:
        fi = yf.Ticker(code).fast_info
        price, prev = float(fi.last_price), float(fi.previous_close)
        return {"price": price, "prev": prev, "change": price - prev, "change_pct": (price / prev - 1) * 100 if prev else None,
                "name": INDICES.get(code), "currency": fi.currency, "open": us_market_open(),
                "time": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")}
    except Exception as e:
        log.warning("yahoo quote failed for %s: %s", code, e)
        return None


def get(codes: list[str]) -> dict[str, dict]:
    """{code: {price, prev, change, change_pct, name, currency, open, time}} for the codes we could fetch."""
    now = time.time()
    want = list(dict.fromkeys(c for c in codes if c))
    with _lock:
        fresh = {c: v for c in want if (v := _cache.get(c)) and now - v[0] < TTL}
    missing = [c for c in want if c not in fresh]
    got: dict[str, dict | None] = {}
    kr = [c for c in missing if is_kr(c)]
    if kr:
        got.update(_naver(kr))
    us = [c for c in missing if not is_kr(c)]
    if us:
        with ThreadPoolExecutor(max_workers=6) as ex:
            got.update(zip(us, ex.map(_yahoo_one, us)))
    with _lock:
        for c in missing:
            _cache[c] = (now, got.get(c))
        out = {c: _cache[c][1] for c in want if c in _cache}
    return {c: v for c, v in out.items() if v}


def history_kr(code: str, days: int = 250) -> list[tuple[str, float]]:
    """Daily closes for a KRX code (FinanceDataReader / Naver), cached 30 minutes."""
    key = f"hist:{code}"
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]["rows"]
    rows: list[tuple[str, float]] = []
    try:
        import FinanceDataReader as fdr
        import pandas as pd
        df = fdr.DataReader(code, (pd.Timestamp.today() - pd.Timedelta(days=int(days * 1.6))).strftime("%Y-%m-%d"))
        rows = [(d.strftime("%Y-%m-%d"), float(c)) for d, c in df["Close"].dropna().tail(days).items()]
    except Exception as e:
        log.warning("kr history failed for %s: %s", code, e)
    with _lock:
        _cache[key] = (time.time(), {"rows": rows})
    return rows

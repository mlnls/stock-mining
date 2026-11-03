"""Data collection via FinanceDataReader.

US (default, ``market: us``)
* Pool = all S&P 500 + top-N by market cap of NASDAQ and NYSE listings (common stocks only).
* ``DataReader(symbol)`` per ticker (Yahoo source, ~0.3 s/ticker, split-adjusted OHLC).
* Regime index: US500 (S&P 500). Also tracked: IXIC (NASDAQ Composite), VIX.

KR (``market: kr``)
* ``StockListing('KRX')`` → codes, names, market, shares outstanding. (Its price columns can be
  empty — KRX restricts that endpoint — so prices never come from it.)
* ``DataReader(code)`` per ticker (Naver source, ~0.1 s/ticker) for daily OHLCV, incrementally.
* Indices: KS11 (KOSPI), KQ11 (KOSDAQ).
* Market cap = last close × shares; liquidity = 20-day average of close × volume.

pykrx is intentionally not used: KRX data endpoints started requiring a login, which breaks it.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta

import pandas as pd

from .db import DB

log = logging.getLogger(__name__)

_US_SYMBOL = re.compile(r"^[A-Z]{1,5}([.-][A-Z])?$")


def indices(cfg: dict) -> list[str]:
    return list(dict.fromkeys([cfg["regime_index"], *cfg.get("info_indices", [])]))


def _fdr():
    import FinanceDataReader as fdr  # imported lazily: slow import, and tests don't need it
    return fdr


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={c: c.lower() for c in df.columns})
    if "value" not in df.columns:
        df["value"] = df["amount"] if "amount" in df.columns else df["close"] * df["volume"]
    df = df[["open", "high", "low", "close", "volume", "value"]].astype(float)
    df = df[(df["close"] > 0) & (df["open"] > 0)]  # drop halted days (O=H=L=0)
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df


def is_common_stock(code: str, name: str, patterns: list[str]) -> bool:
    if not re.fullmatch(r"\d{5}0", str(code)):  # preferred shares etc. don't end in 0
        return False
    return not any(p in str(name) for p in patterns)


def is_us_common(symbol: str, name: str, patterns: list[str]) -> bool:
    if not _US_SYMBOL.match(str(symbol)):
        return False
    return not any(p.lower() in str(name).lower() for p in patterns)


def refresh_listing_us(db: DB, cfg: dict) -> list[str]:
    u = cfg["universe"]
    fdr = _fdr()
    pats = u["exclude_name_patterns"]
    rows: dict[str, tuple] = {}
    sp = fdr.StockListing("S&P500")
    for r in sp.itertuples():
        rows[r.Symbol.replace(".", "-")] = (r.Name, "S&P500")
    for exch in ("NASDAQ", "NYSE"):
        lst = fdr.StockListing(exch)  # ordered by market cap
        lst = lst[[is_us_common(s, n, pats) for s, n in zip(lst["Symbol"], lst["Name"])]]
        for r in lst.head(u["pool_per_exchange"]).itertuples():
            rows.setdefault(r.Symbol.replace(".", "-"), (r.Name, exch))
    now = datetime.now().isoformat(timespec="seconds")
    db.executemany(
        """INSERT INTO tickers(code,name,market,marcap,in_universe,updated) VALUES(?,?,?,NULL,0,?)
           ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market, updated=excluded.updated""",
        [(sym, name, mkt, now) for sym, (name, mkt) in rows.items()],
    )
    stale = [c for c in (r["code"] for r in db.query("SELECT code FROM tickers")) if c not in rows]
    if stale:  # dropped out of the pool → stop tracking (history stays for backtests)
        db.executemany("DELETE FROM tickers WHERE code=?", [(c,) for c in stale])
    log.info("US pool: %d symbols", len(rows))
    return sorted(rows)


def refresh_listing(db: DB, cfg: dict) -> list[str]:
    """Store the tradable pool in ``tickers`` and return its codes."""
    if cfg["market"] == "us":
        return refresh_listing_us(db, cfg)
    pats = cfg["universe"]["exclude_name_patterns"]
    snap = _fdr().StockListing("KRX")
    snap = snap[[is_common_stock(c, n, pats) for c, n in zip(snap["Code"], snap["Name"])]]
    now = datetime.now().isoformat(timespec="seconds")
    db.executemany(
        """INSERT INTO tickers(code,name,market,marcap,in_universe,updated) VALUES(?,?,?,?,0,?)
           ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market, updated=excluded.updated""",
        [(r.Code, r.Name, r.Market, None, now) for r in snap.itertuples()],
    )
    db.set("shares", json.dumps({str(c): float(n) for c, n in zip(snap["Code"], snap["Stocks"])}))
    # delisted / no longer common → drop from universe
    listed = set(snap["Code"])
    stale = [c for c in (r["code"] for r in db.query("SELECT code FROM tickers")) if c not in listed]
    if stale:
        db.executemany("UPDATE tickers SET in_universe=0 WHERE code=?", [(c,) for c in stale])
    return sorted(listed)


def refresh_universe(db: DB, cfg: dict) -> int:
    """Universe = listed common stocks with marcap ≥ min and 20-day avg traded value ≥ min."""
    u = cfg["universe"]
    shares = pd.Series(json.loads(db.get("shares") or "{}"), dtype=float)
    recent = db.df("""SELECT code, date, close, value FROM prices
                      WHERE date >= (SELECT MIN(date) FROM (SELECT DISTINCT date FROM prices ORDER BY date DESC LIMIT 20))""")
    if recent.empty:
        return 0
    g = recent.sort_values("date").groupby("code")
    stat = pd.DataFrame({"close": g["close"].last(), "avg_value": g["value"].mean(), "last": g["date"].last()})
    last_day = recent["date"].max()
    stat = stat[stat["last"] == last_day]  # must have traded on the latest day
    stat["marcap"] = stat["close"] * shares.reindex(stat.index).fillna(0).values if shares.size else float("inf")
    listed = {r["code"] for r in db.query("SELECT code FROM tickers")}
    stat = stat[stat.index.isin(listed)]
    keep = stat[(stat["marcap"] >= u["min_marcap"]) & (stat["avg_value"] >= u["min_avg_value_20"])
                & (stat["close"] >= u.get("min_price", 0))]
    db.execute("UPDATE tickers SET in_universe=0")
    if shares.size:
        db.executemany("UPDATE tickers SET marcap=? WHERE code=?",
                       [(float(m), c) for c, m in stat["marcap"].items()])
    db.executemany("UPDATE tickers SET in_universe=1 WHERE code=?", [(c,) for c in keep.index])
    log.info("universe: %d of %d listed", len(keep), len(listed))
    return len(keep)


def fetch_history(code: str, start: str, retries: int = 3) -> pd.DataFrame:
    for attempt in range(retries):
        try:
            return _normalize(_fdr().DataReader(code, start))
        except Exception as e:  # network hiccups are common; back off and retry
            if "404" in str(e):  # unknown symbol — retrying won't help
                log.warning("fetch %s: symbol not found", code)
                break
            log.warning("fetch %s failed (%s), retry %d", code, e, attempt + 1)
            time.sleep(2 * (attempt + 1))
    return pd.DataFrame()


def update_indices(db: DB, cfg: dict) -> None:
    years = cfg["universe"]["history_years"]
    for code in indices(cfg):
        last = db.query("SELECT MAX(date) d FROM index_prices WHERE code=?", (code,))[0]["d"]
        start = (pd.Timestamp(last) - timedelta(days=7)).strftime("%Y-%m-%d") if last else \
            (date.today() - timedelta(days=365 * years + 400)).isoformat()
        df = fetch_history(code, start)
        if not df.empty:
            db.upsert_index(code, df.drop(columns="value"))


def update_prices(db: DB, cfg: dict, codes: list[str], sleep: float = 0.05) -> int:
    """Incremental download: each ticker from (its last stored date − 7 days), or full history if new.
    Re-downloading the overlap also repairs a partial bar stored during market hours."""
    years = cfg["universe"]["history_years"]
    default_start = (date.today() - timedelta(days=365 * years + 400)).isoformat()  # +warm-up for 200d MAs
    last_by_code = {r["code"]: r["d"] for r in db.query("SELECT code, MAX(date) d FROM prices GROUP BY code")}
    total = 0
    t0 = time.time()
    for i, code in enumerate(codes):
        last = last_by_code.get(code)
        start = (pd.Timestamp(last) - timedelta(days=7)).strftime("%Y-%m-%d") if last else default_start
        total += db.upsert_prices(code, fetch_history(code, start))
        if i % 250 == 0:
            log.info("prices %d/%d (%.0fs)", i, len(codes), time.time() - t0)
        time.sleep(sleep)
    return total


def daily_update(db: DB, cfg: dict, trade_date: str) -> dict:
    """Evening update: indices → listing → prices for all common stocks → universe."""
    update_indices(db, cfg)
    idx_last = db.query("SELECT MAX(date) d FROM index_prices WHERE code=?", (cfg["regime_index"],))[0]["d"]
    if idx_last != trade_date:
        return {"ok": False, "reason": f"지수에 {trade_date} 데이터 없음 (최근 {idx_last}) — 휴장 또는 데이터 지연"}
    codes = refresh_listing(db, cfg)
    n = update_prices(db, cfg, codes)
    size = refresh_universe(db, cfg)
    return {"ok": True, "rows": n, "universe": size}

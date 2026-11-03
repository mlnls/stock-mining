"""SQLite storage: prices, strategies, signals, paper trades, journal, proposals, reports."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickers (
    code TEXT PRIMARY KEY,
    name TEXT,
    market TEXT,
    marcap REAL,
    in_universe INTEGER DEFAULT 0,
    updated TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, value REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

-- market indices (US500, IXIC, VIX / KS11, KQ11)
CREATE TABLE IF NOT EXISTS index_prices (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS strategies (
    name TEXT PRIMARY KEY,
    spec TEXT NOT NULL,            -- JSON rule spec (see rules.py)
    source TEXT NOT NULL,          -- builtin | claude | user
    status TEXT NOT NULL,          -- active | muted | retired
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    backtest TEXT                  -- JSON metrics of last full backtest
);

CREATE TABLE IF NOT EXISTS signals (
    date TEXT NOT NULL,            -- signal day (close); order is for next session
    strategy TEXT NOT NULL,
    code TEXT NOT NULL,
    score REAL,
    close REAL, atr REAL,
    stop REAL, target REAL,
    PRIMARY KEY (date, strategy, code)
);

CREATE TABLE IF NOT EXISTS recommendations (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    strategy TEXT NOT NULL,
    combined_score REAL,
    entry_ref REAL, stop REAL, target REAL,
    shares INTEGER,
    flagged TEXT,                   -- reason if morning research said avoid
    PRIMARY KEY (date, code)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    code TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    regime TEXT,
    atr REAL,
    entry_date TEXT, entry_price REAL,
    stop REAL, target REAL,
    exit_date TEXT, exit_price REAL, exit_reason TEXT,
    ret_pct REAL,
    status TEXT NOT NULL,          -- pending | open | closed | expired
    UNIQUE(strategy, code, signal_date)
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,            -- buy | sell | note
    price REAL, qty INTEGER,
    plan_stop REAL, plan_target REAL,
    strategy TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,
    kind TEXT NOT NULL,            -- principle | rule | params
    title TEXT NOT NULL,
    rationale TEXT,
    payload TEXT,                  -- principle text or JSON rule spec / params
    backtest TEXT,                 -- JSON metrics
    passed INTEGER,                -- backtest gate result (NULL for pure principle text)
    status TEXT NOT NULL,          -- pending | approved | rejected | applied
    decided TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,            -- morning | evening | weekly | ask
    body TEXT NOT NULL
);

-- learning log: what the system (Claude + edge map) has noticed, and whether it held up
CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,          -- evening | weekly | edgemap
    kind TEXT NOT NULL,            -- observation | hypothesis | lesson
    text TEXT NOT NULL,
    status TEXT NOT NULL,          -- open | confirmed | rejected
    note TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=60)
        self.conn.row_factory = sqlite3.Row
        # Switching to WAL ignores the busy timeout, so two processes opening a fresh DB at once
        # (bot + init right after install) can fail with "database is locked". WAL persists, so retry.
        for attempt in range(30):
            try:
                if self.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                    self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.executescript(SCHEMA)
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or attempt == 29:
                    raise
                time.sleep(1)

    # ------------------------------------------------------------------ generic
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return cur

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        self.conn.executemany(sql, rows)
        self.conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def df(self, sql: str, params: Iterable[Any] = ()) -> pd.DataFrame:
        return pd.read_sql_query(sql, self.conn, params=tuple(params))

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        self.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))

    # ------------------------------------------------------------------ prices
    def upsert_prices(self, code: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        rows = [
            (code, d, r.open, r.high, r.low, r.close, r.volume, r.value)
            for d, r in zip(df.index.strftime("%Y-%m-%d"), df.itertuples())
        ]
        self.executemany(
            "INSERT OR REPLACE INTO prices(code,date,open,high,low,close,volume,value) VALUES(?,?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    def upsert_index(self, code: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        rows = [
            (code, d, r.open, r.high, r.low, r.close, r.volume)
            for d, r in zip(df.index.strftime("%Y-%m-%d"), df.itertuples())
        ]
        self.executemany(
            "INSERT OR REPLACE INTO index_prices(code,date,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    def last_price_date(self, code: str | None = None) -> str | None:
        if code:
            row = self.conn.execute("SELECT MAX(date) FROM prices WHERE code=?", (code,)).fetchone()
        else:
            row = self.conn.execute("SELECT MAX(date) FROM prices").fetchone()
        return row[0] if row else None

    def load_panel(self, codes: list[str] | None = None, start: str | None = None) -> dict[str, pd.DataFrame]:
        """Return {code: OHLCV DataFrame indexed by Timestamp}."""
        sql = "SELECT code,date,open,high,low,close,volume,value FROM prices"
        where, params = [], []
        if codes is not None:
            if not codes:
                return {}
            where.append(f"code IN ({','.join('?' * len(codes))})")
            params += codes
        if start:
            where.append("date >= ?")
            params.append(start)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY code, date"
        df = self.df(sql, params)
        if df.empty:
            return {}
        df["date"] = pd.to_datetime(df["date"])
        return {c: g.drop(columns="code").set_index("date") for c, g in df.groupby("code", sort=False)}

    def load_index(self, code: str, start: str | None = None) -> pd.DataFrame:
        sql = "SELECT date,open,high,low,close,volume FROM index_prices WHERE code=?"
        params: list[Any] = [code]
        if start:
            sql += " AND date >= ?"
            params.append(start)
        df = self.df(sql + " ORDER BY date", params)
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date")

    def universe(self) -> list[str]:
        return [r["code"] for r in self.query("SELECT code FROM tickers WHERE in_universe=1 ORDER BY code")]

    def ticker_info(self) -> dict[str, dict]:
        return {r["code"]: r for r in self.query("SELECT * FROM tickers")}

    # ------------------------------------------------------------------ strategies
    def strategies(self, statuses: tuple[str, ...] = ("active", "muted")) -> list[dict]:
        rows = self.query(
            f"SELECT * FROM strategies WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY name", statuses
        )
        for r in rows:
            r["spec"] = json.loads(r["spec"])
            r["backtest"] = json.loads(r["backtest"]) if r["backtest"] else None
        return rows

    def save_strategy(self, spec: dict, source: str, status: str, now: str, backtest: dict | None = None) -> None:
        self.execute(
            """INSERT INTO strategies(name,spec,source,status,created,updated,backtest) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET spec=excluded.spec, status=excluded.status,
               updated=excluded.updated, backtest=COALESCE(excluded.backtest, strategies.backtest)""",
            (spec["name"], json.dumps(spec, ensure_ascii=False), source, status, now, now,
             json.dumps(backtest) if backtest else None),
        )

    def add_report(self, kind: str, body: str, ts: str) -> None:
        self.execute("INSERT INTO reports(ts,kind,body) VALUES(?,?,?)", (ts, kind, body))

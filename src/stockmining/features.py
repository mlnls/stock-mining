"""Indicator features and a small, safe expression language over them.

Rule conditions are strings such as ``"close > sma_50 and rsi_2 < 10"``. They are parsed
with :mod:`ast` and only a whitelist of node types is accepted, so specs written by Claude
(or anyone) can never execute arbitrary code.

Feature names (N is an integer window):
  open high low close volume value      raw daily bar (value = 거래대금, KRW)
  sma_N ema_N                           moving averages of close
  vma_N                                 volume SMA;  vavg_N  value(거래대금) SMA
  rsi_N                                 Wilder RSI
  atr_N natr_N                          ATR and ATR as % of close
  hh_N ll_N                             highest high / lowest low of the PREVIOUS N days (today excluded)
  hc_N lc_N                             highest / lowest close of the previous N days
  ret_N                                 % change of close over N days
  vol_ratio_N                           volume / previous N-day average volume
  bbu_N bbl_N bbw_N                     Bollinger upper / lower (2σ) / width %
  range body gap                        high-low, |close-open|, % gap open vs previous close
  up_days_N                             count of up closes in last N days
  xrank_<feature>                       cross-sectional percentile (0-100) of a feature across the universe that day
  ml_score                              machine-learning score 0-100: daily percentile of a walk-forward gradient
                                        boosting model's prediction of the next-10-day relative return (see ml.py)
Functions: prev(x, n), cross_up(a, b), cross_down(a, b), abs(x), min(a, b), max(a, b)
"""
from __future__ import annotations

import ast
import re
from functools import lru_cache

import numpy as np
import pandas as pd

_FEATURE_RE = re.compile(
    r"^(sma|ema|vma|vavg|rsi|atr|natr|hh|ll|hc|lc|ret|vol_ratio|bbu|bbl|bbw|up_days)_(\d+)$"
)
_BASE = {"open", "high", "low", "close", "volume", "value", "range", "body", "gap", "ml_score"}
_FUNCS = {"prev", "cross_up", "cross_down", "abs", "min", "max"}


class ExprError(ValueError):
    pass


def is_feature_name(name: str) -> bool:
    if name.startswith("xrank_"):
        return is_feature_name(name[len("xrank_"):])
    return name in _BASE or bool(_FEATURE_RE.match(name))


def _rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.where(loss != 0, 100.0)


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def compute_feature(df: pd.DataFrame, name: str) -> pd.Series:
    """Compute (and cache in ``df``) a single per-ticker feature column."""
    if name in df.columns:
        return df[name]
    if name.startswith("xrank_") or name == "ml_score":
        raise ExprError(f"{name} is cross-sectional; call add_cross_sectional() on the panel first")
    c = df["close"]
    if name == "range":
        s = df["high"] - df["low"]
    elif name == "body":
        s = (df["close"] - df["open"]).abs()
    elif name == "gap":
        s = (df["open"] / c.shift(1) - 1) * 100
    else:
        m = _FEATURE_RE.match(name)
        if not m:
            raise ExprError(f"unknown feature: {name}")
        kind, n = m.group(1), int(m.group(2))
        if n <= 0 or n > 400:
            raise ExprError(f"window out of range in {name}")
        if kind == "sma":
            s = c.rolling(n, min_periods=n).mean()
        elif kind == "ema":
            s = c.ewm(span=n, adjust=False, min_periods=n).mean()
        elif kind == "vma":
            s = df["volume"].rolling(n, min_periods=n).mean()
        elif kind == "vavg":
            s = df["value"].rolling(n, min_periods=n).mean()
        elif kind == "rsi":
            s = _rsi(c, n)
        elif kind == "atr":
            s = _atr(df, n)
        elif kind == "natr":
            s = _atr(df, n) / c * 100
        elif kind == "hh":
            s = df["high"].shift(1).rolling(n, min_periods=n).max()
        elif kind == "ll":
            s = df["low"].shift(1).rolling(n, min_periods=n).min()
        elif kind == "hc":
            s = c.shift(1).rolling(n, min_periods=n).max()
        elif kind == "lc":
            s = c.shift(1).rolling(n, min_periods=n).min()
        elif kind == "ret":
            s = (c / c.shift(n) - 1) * 100
        elif kind == "vol_ratio":
            s = df["volume"] / df["volume"].shift(1).rolling(n, min_periods=n).mean()
        elif kind in ("bbu", "bbl", "bbw"):
            mid = c.rolling(n, min_periods=n).mean()
            sd = c.rolling(n, min_periods=n).std()
            s = {"bbu": mid + 2 * sd, "bbl": mid - 2 * sd, "bbw": 4 * sd / mid * 100}[kind]
        elif kind == "up_days":
            s = (c.diff() > 0).astype(float).rolling(n, min_periods=n).sum()
        else:  # pragma: no cover
            raise ExprError(name)
    s = s.replace([np.inf, -np.inf], np.nan)
    df[name] = s
    return s


# ---------------------------------------------------------------------------- parsing
_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Compare, ast.Gt, ast.GtE, ast.Lt, ast.LtE,
    ast.Name, ast.Load, ast.Constant, ast.Call,
)


@lru_cache(maxsize=4096)
def parse(expr: str) -> ast.Expression:
    if len(expr) > 500:
        raise ExprError("expression too long")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise ExprError(f"syntax error in {expr!r}: {e.msg}") from e
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExprError(f"disallowed syntax {type(node).__name__} in {expr!r}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise ExprError(f"only numeric constants allowed in {expr!r}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS or node.keywords:
                raise ExprError(f"unknown function in {expr!r}")
        if isinstance(node, ast.Name):
            # function names appear as Name nodes too
            if node.id not in _FUNCS and not is_feature_name(node.id):
                raise ExprError(f"unknown feature {node.id!r} in {expr!r}")
    return tree


def names_in(expr: str) -> set[str]:
    tree = parse(expr)
    funcs = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id not in funcs}


def evaluate(df: pd.DataFrame, expr: str) -> pd.Series:
    """Evaluate an expression on one ticker's DataFrame → Series (bool or float)."""
    tree = parse(expr)

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            return compute_feature(df, node.id)
        if isinstance(node, ast.UnaryOp):
            v = ev(node.operand)
            if isinstance(node.op, ast.USub):
                return -v
            if isinstance(node.op, ast.UAdd):
                return v
            return ~_as_bool(v, df)
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            if isinstance(node.op, ast.Div):
                if isinstance(b, pd.Series):
                    b = b.replace(0, np.nan)
                elif b == 0:
                    return pd.Series(np.nan, index=df.index)
                return a / b
        if isinstance(node, ast.BoolOp):
            vals = [_as_bool(ev(v), df) for v in node.values]
            out = vals[0]
            for v in vals[1:]:
                out = (out & v) if isinstance(node.op, ast.And) else (out | v)
            return out
        if isinstance(node, ast.Compare):
            left = ev(node.left)
            out = None
            for op, comp in zip(node.ops, node.comparators):
                right = ev(comp)
                if isinstance(op, ast.Gt):
                    r = left > right
                elif isinstance(op, ast.GtE):
                    r = left >= right
                elif isinstance(op, ast.Lt):
                    r = left < right
                else:
                    r = left <= right
                r = _as_bool(r, df)
                out = r if out is None else (out & r)
                left = right
            return out
        if isinstance(node, ast.Call):
            fn = node.func.id
            args = [ev(a) for a in node.args]
            if fn == "prev":
                if len(args) != 2 or not isinstance(args[1], float):
                    raise ExprError("prev(x, n) needs a numeric n")
                return _series(args[0], df).shift(int(args[1]))
            if fn in ("cross_up", "cross_down"):
                if len(args) != 2:
                    raise ExprError(f"{fn}(a, b) takes two args")
                a, b = _series(args[0], df), _series(args[1], df)
                if fn == "cross_up":
                    return (a > b) & (a.shift(1) <= b.shift(1))
                return (a < b) & (a.shift(1) >= b.shift(1))
            if fn == "abs":
                return abs(args[0])
            if fn in ("min", "max"):
                if len(args) != 2:
                    raise ExprError(f"{fn}(a, b) takes two args")
                a, b = _series(args[0], df), _series(args[1], df)
                return np.minimum(a, b) if fn == "min" else np.maximum(a, b)
        raise ExprError(f"cannot evaluate {ast.dump(node)}")  # pragma: no cover

    return ev(tree)


def _series(v, df) -> pd.Series:
    return v if isinstance(v, pd.Series) else pd.Series(v, index=df.index, dtype=float)


def _as_bool(v, df) -> pd.Series:
    s = _series(v, df)
    if s.dtype != bool:
        s = s.fillna(0).astype(bool)
    return s


# ---------------------------------------------------------------------------- panel helpers
def add_cross_sectional(panel: dict[str, pd.DataFrame], exprs: list[str]) -> None:
    """Compute any ``xrank_*`` features referenced in ``exprs`` across the whole panel."""
    names = {n for e in exprs for n in names_in(e)}
    if "ml_score" in names or "xrank_ml_score" in names:
        from . import ml
        ml.add_ml_score(panel)
    needed = {n for n in names if n.startswith("xrank_")}
    for name in needed:
        base = name[len("xrank_"):]
        wide = pd.DataFrame({code: compute_feature(df, base) for code, df in panel.items()})
        ranks = wide.rank(axis=1, pct=True) * 100
        for code, df in panel.items():
            df[name] = ranks[code].reindex(df.index)

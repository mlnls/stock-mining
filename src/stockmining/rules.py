"""Strategy rule specs (a small JSON/YAML DSL) and their evaluation.

Spec shape::

    name: trend_pullback
    description: "..."
    regimes: [bull, neutral]          # market regimes in which new entries are allowed
    params: {rsi_th: 10}              # substituted into "{rsi_th}" placeholders
    param_grid: {rsi_th: [5, 10, 15]} # optional: values the weekly tuner may try
    entry:
      type: next_open                 # next_open | breakout (enter next day at open + k*range)
      k: 0.5                          # breakout only
      all: ["close > sma_200", "rsi_2 < {rsi_th}"]
      any: []                         # optional: at least one must hold
    rank: "-rsi_2"                    # higher = better candidate
    exit:
      stop_atr: 2.0                   # or stop_pct: 7
      target_atr: 3.0                 # or target_pct: 15 (optional)
      trail_atr: null                 # trailing stop from highest close since entry
      max_hold: 10                    # trading days; exit next open after N days
      exit_when: ["close > sma_5"]    # any true at close -> exit next open
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import features as F

REGIMES = ("bull", "neutral", "bear")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


class SpecError(ValueError):
    pass


def _subst(expr: str, params: dict) -> str:
    def rep(m):
        key = m.group(1)
        if key not in params:
            raise SpecError(f"missing param {key!r} for {expr!r}")
        return repr(params[key])

    return _PLACEHOLDER.sub(rep, expr)


def resolve(spec: dict, params: dict | None = None) -> dict:
    """Return a copy of spec with placeholders replaced by params (spec params overridden by ``params``)."""
    p = dict(spec.get("params") or {})
    p.update(params or {})
    out = copy.deepcopy(spec)
    out["params"] = p
    ent = out.setdefault("entry", {})
    ent["all"] = [_subst(e, p) for e in ent.get("all") or []]
    ent["any"] = [_subst(e, p) for e in ent.get("any") or []]
    if out.get("rank"):
        out["rank"] = _subst(out["rank"], p)
    ex = out.setdefault("exit", {})
    ex["exit_when"] = [_subst(e, p) for e in ex.get("exit_when") or []]
    for key in ("stop_atr", "stop_pct", "target_atr", "target_pct", "trail_atr", "max_hold", "k"):
        for d in (ex, ent):
            if isinstance(d.get(key), str):
                d[key] = float(_subst(d[key], p))
    return out


def validate(spec: dict) -> dict:
    """Validate a spec (raises SpecError). Returns the resolved spec."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object")
    name = spec.get("name", "")
    if not _NAME_RE.match(name):
        raise SpecError("name must be snake_case, 3-41 chars")
    r = resolve(spec)
    ent, ex = r["entry"], r["exit"]
    if not ent["all"] and not ent["any"]:
        raise SpecError("entry needs at least one condition")
    if ent.get("type", "next_open") not in ("next_open", "breakout"):
        raise SpecError("entry.type must be next_open or breakout")
    if ent.get("type") == "breakout" and not (0 < float(ent.get("k", 0)) <= 2):
        raise SpecError("breakout entry needs 0 < k <= 2")
    for e in ent["all"] + ent["any"] + ex["exit_when"] + ([r["rank"]] if r.get("rank") else []):
        try:
            F.parse(e)
        except F.ExprError as err:
            raise SpecError(str(err)) from err
    if not (ex.get("stop_atr") or ex.get("stop_pct")):
        raise SpecError("exit needs stop_atr or stop_pct (every rule must have a stop)")
    mh = ex.get("max_hold")
    if not mh or not (1 <= int(mh) <= 120):
        raise SpecError("exit.max_hold must be 1..120 trading days")
    for rg in r.get("regimes") or []:
        if rg not in REGIMES:
            raise SpecError(f"unknown regime {rg}")
    for k, vals in (spec.get("param_grid") or {}).items():
        if k not in r["params"] or not isinstance(vals, list) or not vals:
            raise SpecError(f"param_grid key {k} must exist in params and be a non-empty list")
    return r


def all_exprs(spec: dict) -> list[str]:
    r = resolve(spec)
    return r["entry"]["all"] + r["entry"]["any"] + r["exit"]["exit_when"] + ([r["rank"]] if r.get("rank") else [])


# ---------------------------------------------------------------------------- signals
def entry_mask(df: pd.DataFrame, spec: dict, min_value: float = 0) -> pd.Series:
    """Boolean Series: True on days whose close triggers an entry for the next session.
    ``min_value`` applies the liquidity filter point-in-time (20-day avg traded value as of that day),
    so backtests don't pick stocks by how liquid they are *today* (look-ahead bias)."""
    ent = spec["entry"]
    mask = pd.Series(True, index=df.index)
    if min_value:
        mask &= F.compute_feature(df, "vavg_20").fillna(0) >= min_value
    for e in ent["all"]:
        mask &= F._as_bool(F.evaluate(df, e), df)
    if ent["any"]:
        anym = pd.Series(False, index=df.index)
        for e in ent["any"]:
            anym |= F._as_bool(F.evaluate(df, e), df)
        mask &= anym
    # need enough history and a valid ATR for stop placement
    mask &= F.compute_feature(df, "atr_14").notna()
    return mask


def rank_series(df: pd.DataFrame, spec: dict) -> pd.Series:
    if spec.get("rank"):
        return F._series(F.evaluate(df, spec["rank"]), df).astype(float)
    return F.compute_feature(df, "vavg_20")  # default: liquidity


def exit_when_mask(df: pd.DataFrame, spec: dict) -> np.ndarray:
    m = pd.Series(False, index=df.index)
    for e in spec["exit"]["exit_when"]:
        m |= F._as_bool(F.evaluate(df, e), df)
    return m.to_numpy()


def stop_target(entry: float, atr: float, ex: dict) -> tuple[float, float | None]:
    stop = entry - ex["stop_atr"] * atr if ex.get("stop_atr") else entry * (1 - ex["stop_pct"] / 100)
    target = None
    if ex.get("target_atr"):
        target = entry + ex["target_atr"] * atr
    elif ex.get("target_pct"):
        target = entry * (1 + ex["target_pct"] / 100)
    return stop, target


@dataclass
class Exit:
    idx: int | None        # index of exit bar (None = still open)
    price: float | None
    reason: str | None
    stop: float            # last stop level (moves with trailing)


def simulate_exit(
    o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, atr: np.ndarray, exit_flag: np.ndarray,
    entry_idx: int, entry_price: float, stop: float, target: float | None, ex: dict,
    intrabar_entry: bool = False,
) -> Exit:
    """Walk forward bar by bar from the entry bar. Conservative: if stop and target touch the
    same bar, assume the stop hit first. Signal-type exits (exit_when / max_hold) fill at the
    next bar's open. With ``intrabar_entry`` (breakout fills) the entry bar's low/high may have
    printed before the fill, so on that bar only the close is checked against the stop."""
    n = len(c)
    max_hold = int(ex["max_hold"])
    trail = ex.get("trail_atr")
    highest_close = entry_price
    pending_open_exit: str | None = None
    for i in range(entry_idx, n):
        if pending_open_exit:
            return Exit(i, o[i], pending_open_exit, stop)
        if intrabar_entry and i == entry_idx:
            if c[i] <= stop:
                return Exit(i, c[i], "stop", stop)
        elif i > entry_idx and o[i] <= stop:
            return Exit(i, o[i], "stop_gap", stop)
        elif l[i] <= stop:
            return Exit(i, min(stop, o[i]) if i > entry_idx else stop, "stop", stop)
        if target is not None and not (intrabar_entry and i == entry_idx):
            if i > entry_idx and o[i] >= target:
                return Exit(i, o[i], "target_gap", stop)
            if h[i] >= target:
                return Exit(i, target, "target", stop)
        # end-of-bar updates
        highest_close = max(highest_close, c[i])
        if trail and not np.isnan(atr[i]):
            stop = max(stop, highest_close - trail * atr[i])
        held = i - entry_idx + 1
        if exit_flag[i]:
            pending_open_exit = "rule"
        elif held >= max_hold:
            pending_open_exit = "time"
    return Exit(None, None, None, stop)


def entry_fill(o: np.ndarray, h: np.ndarray, sig_idx: int, rng: float, ent: dict) -> float | None:
    """Fill price on the bar after the signal bar, or None if not filled."""
    i = sig_idx + 1
    if i >= len(o):
        return None
    if ent.get("type", "next_open") == "breakout":
        level = o[i] + float(ent["k"]) * rng
        return level if h[i] >= level else None
    return o[i]

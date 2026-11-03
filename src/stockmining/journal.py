"""Trade journal (매매일지): real trades you report via Telegram, positions, discipline checks."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime

import pandas as pd

from .db import DB


def record(db: DB, side: str, code: str, price: float | None, qty: int | None, note: str = "",
           date: str | None = None) -> dict:
    now = datetime.now()
    date = date or now.strftime("%Y-%m-%d")
    plan_stop = plan_target = strategy = None
    if side == "buy":
        rec = db.query(
            "SELECT * FROM recommendations WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1", (code, date))
        if rec and (pd.Timestamp(date) - pd.Timestamp(rec[0]["date"])).days <= 5:
            r = rec[0]
            # re-anchor stop/target distance to the actual fill price
            plan_stop = price - (r["entry_ref"] - r["stop"]) if price else r["stop"]
            plan_target = price + (r["target"] - r["entry_ref"]) if price and r["target"] else r["target"]
            strategy = r["strategy"]
    db.execute(
        """INSERT INTO journal(ts,date,code,side,price,qty,plan_stop,plan_target,strategy,note)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (now.isoformat(timespec="seconds"), date, code, side, price, qty, plan_stop, plan_target, strategy, note),
    )
    return {"plan_stop": plan_stop, "plan_target": plan_target, "strategy": strategy}


def set_stop(db: DB, code: str, stop: float) -> bool:
    row = db.query("SELECT id FROM journal WHERE code=? AND side='buy' ORDER BY id DESC LIMIT 1", (code,))
    if not row:
        return False
    db.execute("UPDATE journal SET plan_stop=? WHERE id=?", (stop, row[0]["id"]))
    return True


def positions_and_roundtrips(db: DB) -> tuple[dict[str, dict], list[dict]]:
    """FIFO matching of buys and sells → open positions and closed round trips."""
    lots: dict[str, deque] = defaultdict(deque)
    trips: list[dict] = []
    for j in db.query("SELECT * FROM journal WHERE side IN ('buy','sell') ORDER BY ts, id"):
        code, qty = j["code"], j["qty"] or 0
        if j["side"] == "buy":
            lots[code].append(dict(j, remaining=qty))
            continue
        while qty > 0 and lots[code]:
            lot = lots[code][0]
            take = min(qty, lot["remaining"])
            trips.append({
                "code": code, "buy_date": lot["date"], "sell_date": j["date"], "qty": take,
                "buy": lot["price"], "sell": j["price"], "plan_stop": lot["plan_stop"],
                "plan_target": lot["plan_target"], "strategy": lot["strategy"],
                "ret_pct": round((j["price"] / lot["price"] - 1) * 100, 2) if lot["price"] and j["price"] else None,
                "buy_note": lot["note"], "sell_note": j["note"],
            })
            lot["remaining"] -= take
            qty -= take
            if lot["remaining"] == 0:
                lots[code].popleft()
    positions = {}
    for code, dq in lots.items():
        q = sum(l["remaining"] for l in dq)
        if q <= 0:
            continue
        cost = sum(l["remaining"] * (l["price"] or 0) for l in dq) / q
        last = dq[-1]
        positions[code] = {"code": code, "qty": q, "avg_price": round(cost, 4), "first_buy": dq[0]["date"],
                           "plan_stop": last["plan_stop"], "plan_target": last["plan_target"],
                           "strategy": last["strategy"]}
    return positions, trips


def discipline(db: DB, trips: list[dict], cfg: dict, n: int = 10) -> dict:
    """Rule-adherence checks on the last ``n`` round trips — the raw material for Claude's critique."""
    recent = trips[-n:]
    issues = []
    late_stop = no_plan = held_long = oversize = 0
    for t in recent:
        if not t["strategy"]:
            no_plan += 1
            issues.append(f"{t['code']} {t['buy_date']}: 시스템 추천 없이 매수")
        if t["plan_stop"] and t["sell"] and t["sell"] < t["plan_stop"] * 0.99:
            late_stop += 1
            issues.append(f"{t['code']} {t['sell_date']}: 손절가 {t['plan_stop']:.2f} 대비 늦은 청산 {t['sell']:.2f}")
        days = (pd.Timestamp(t["sell_date"]) - pd.Timestamp(t["buy_date"])).days
        if days > 60:
            held_long += 1
            issues.append(f"{t['code']}: {days}일 보유 (시간 손절 초과 가능성)")
        if t["buy"] and t["qty"] and t["buy"] * t["qty"] > cfg["account"]["capital"] * cfg["account"]["max_position_weight"] * 1.1:
            oversize += 1
            issues.append(f"{t['code']} {t['buy_date']}: 비중 한도 초과 매수")
    rets = [t["ret_pct"] for t in recent if t["ret_pct"] is not None]
    return {
        "recent_trips": len(recent),
        "late_stop": late_stop, "no_signal_buys": no_plan, "held_too_long": held_long, "oversized": oversize,
        "win_rate": round(sum(r > 0 for r in rets) / len(rets) * 100, 1) if rets else None,
        "avg_ret_pct": round(sum(rets) / len(rets), 2) if rets else None,
        "issues": issues,
    }


def open_position_issues(positions: dict[str, dict], cfg: dict) -> list[str]:
    """Rule checks on positions still held (round-trip checks only see closed trades)."""
    acct = cfg["account"]
    issues = []
    for code, p in positions.items():
        if not p.get("plan_stop"):
            issues.append(f"{code}: 손절가 미등록 → /stop {code} 가격")
        if not p.get("strategy"):
            issues.append(f"{code}: 시스템 추천 없이 매수한 보유 종목")
        value = (p.get("last_close") or p["avg_price"]) * p["qty"]
        if value > acct["capital"] * acct["max_position_weight"] * 1.1:
            issues.append(f"{code}: 평가액 {value:,.0f}이 비중 한도 {acct['max_position_weight']:.0%} 초과")
    return issues


def position_alerts(positions: dict[str, dict], panel: dict[str, pd.DataFrame], fmt=lambda p: f"{p:,.2f}") -> list[str]:
    alerts = []
    for code, p in positions.items():
        df = panel.get(code)
        if df is None or df.empty:
            continue
        close = float(df["close"].iloc[-1])
        p["last_close"] = close
        p["pnl_pct"] = round((close / p["avg_price"] - 1) * 100, 2) if p["avg_price"] else None
        if p["plan_stop"] and close <= p["plan_stop"]:
            alerts.append(f"⚠️ {code} 종가 {fmt(close)} ≤ 손절가 {fmt(p['plan_stop'])} → 다음 거래일 시가 청산 원칙")
        elif p["plan_target"] and close >= p["plan_target"]:
            alerts.append(f"🎯 {code} 목표가 {fmt(p['plan_target'])} 도달 (종가 {fmt(close)})")
    return alerts

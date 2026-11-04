"""Read-only web dashboard: ``stockmining web`` (stdlib HTTP server, server-rendered HTML).

Mobile-first list-and-card layout: big numbers first, tap a row to expand its chart and details. Korean
colour convention (rise = red, fall = blue). The DB is opened read-only; access needs HTTP basic auth with
WEB_PASSWORD (user WEB_USER, default "me"); without a password it only listens on localhost.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import sqlite3
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import journal as J
from . import labels as LB
from .config import Config
from .db import DB

log = logging.getLogger(__name__)


class ReadOnlyDB(DB):
    def __init__(self, path: str | Path):  # skip schema/WAL setup: never write from the web process
        self.path = Path(path)
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
        self.conn.row_factory = sqlite3.Row


# ---------------------------------------------------------------------------- formatting
def h(v) -> str:
    return escape("" if v is None else str(v))


def money(v, us: bool = True, sign: bool = False) -> str:
    if v is None:
        return "-"
    s = ("+" if v > 0 else "-" if v < 0 else "") if sign else ("-" if v < 0 else "")
    return f"{s}${abs(v):,.2f}" if us else f"{s}{abs(v):,.0f}원"


def pct(v, digits: int = 1, sign: bool = True) -> str:
    if v is None:
        return "-"
    return f"{v:+.{digits}f}%" if sign else f"{v:.{digits}f}%"


def tone(v) -> str:
    """Korean convention: gain = red ("up"), loss = blue ("down")."""
    return "" if v is None or v == 0 else ("up" if v > 0 else "down")


def shares(q) -> str:
    return "-" if q is None else f"{round(q, 2):g}주"


def chart(points: list[tuple[str, float]], stop: float | None = None, avg: float | None = None) -> str:
    """Placeholder the page script turns into a responsive line chart with hover."""
    if len(points) < 2:
        return ""
    data = {"d": [p[0] for p in points], "c": [round(p[1], 4) for p in points], "stop": stop, "avg": avg}
    return f'<div class="chart" data-chart="{h(json.dumps(data))}"></div>'


def series(db: DB, code: str, n: int = 120) -> list[tuple[str, float]]:
    rows = db.query("SELECT date, close FROM prices WHERE code=? AND close IS NOT NULL ORDER BY date DESC LIMIT ?",
                    (code, n))
    return [(r["date"], r["close"]) for r in reversed(rows)]


def kv(items: list[tuple[str, str]]) -> str:
    return '<dl class="kv">' + "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in items) + "</dl>"


def avatar(code: str) -> str:
    return f'<span class="av">{h(code[:4])}</span>'


def row(code: str, title: str, sub: str, right: str, right_sub: str, body: str, open_: bool = False) -> str:
    return (f'<details class="row"{" open" if open_ else ""}><summary>{avatar(code)}'
            f'<span class="rt"><b>{title}</b><small>{sub}</small></span>'
            f'<span class="rr"><b>{right}</b><small>{right_sub}</small></span><i class="chev"></i></summary>'
            f'<div class="rb">{body}</div></details>')


def empty(icon: str, text: str) -> str:
    return f'<div class="empty"><span>{icon}</span>{h(text)}</div>'


def report_html(body: str) -> str:
    """Slack-style text → light HTML: *bold*, <url|label> / [label](url) links, '•'/'-' bullets."""
    t = h(body)
    t = re.sub(r"&lt;(https?://[^|&]+)\|([^&]+)&gt;", r'<a href="\1" target="_blank" rel="noopener">\2</a>', t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', t)
    t = re.sub(r"\*\*(.+?)\*\*|\*(\S[^*\n]*?)\*", lambda m: f"<b>{m.group(1) or m.group(2)}</b>", t)
    t = re.sub(r"(?m)^#{1,6}\s*(.+)$", r"<b>\1</b>", t)
    t = re.sub(r"(?m)^\s*[-•]\s+(.+)\n?", r'<span class="li">\1</span>', t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return f'<div class="report">{t}</div>'


STATUS = {"active": "사용 중", "muted": "쉬는 중", "retired": "퇴역"}


# ---------------------------------------------------------------------------- page
def render(cfg: Config, db: DB) -> str:
    us = cfg.is_us
    info = db.ticker_info()
    name = lambda c: info.get(c, {}).get("name") or c  # noqa: E731
    ctx = json.loads(db.get("last_evening_ctx") or "{}")
    last_day = db.last_price_date() or ""
    acct, cap = cfg["account"], cfg["account"].get("capital") or 0

    from . import regime as RG
    idx = db.load_index(cfg["regime_index"])
    market = RG.describe(idx) if not idx.empty else {}

    # ---- holdings
    pos, trips = J.positions_and_roundtrips(db)
    if pos:
        J.position_alerts(pos, db.load_panel(list(pos), None))
    value = sum(p["qty"] * (p.get("last_close") or p["avg_price"]) for p in pos.values())
    cost = sum(p["qty"] * p["avg_price"] for p in pos.values())
    pnl = value - cost if pos else None
    realized = sum((t["sell"] - t["buy"]) * t["qty"] for t in trips if t["sell"] and t["buy"])

    # ---- hero
    d = last_day[5:].replace("-", "/") if last_day else ""
    hero_sub = (f'<span class="{tone(pnl)}">{money(pnl, us, True)} ({pct(pnl / cost * 100 if cost else 0, 2)})</span>'
                f' · 보유 {len(pos)}종목' if pos else "아직 보유한 종목이 없어요")
    chips = [f'시장 <b>{h(LB.regime(market.get("regime")))}</b>',
             f'{"S&P500" if us else h(cfg["regime_index"])} 한 달 '
             f'<b class="{tone(market.get("chg_20d_pct"))}">{pct(market.get("chg_20d_pct"))}</b>',
             f'<b>{h({"aggressive": "공격형", "normal": "기본형"}.get(cfg["profile"], cfg["profile"]))}</b>']
    if trips:
        chips.append(f'실현 손익 <b class="{tone(realized)}">{money(realized, us, True)}</b>')
    hero = (f'<section class="hero"><small>{h(d)} 장 마감 기준</small><span>운용 자금</span>'
            f'<b class="big">{money(cap, us)}</b><p>{hero_sub}</p>'
            f'<div class="chips">{"".join(f"<span>{c}</span>" for c in chips)}</div></section>')

    alerts = "".join(f'<div class="alert">{h(a)}</div>' for a in ctx.get("alerts") or [])

    # ---- tomorrow's candidates
    rdate = db.query("SELECT MAX(date) d FROM recommendations")[0]["d"]
    recs = db.query("SELECT * FROM recommendations WHERE date=? ORDER BY combined_score DESC", (rdate,)) if rdate else []
    earn = {r["code"]: r.get("next_earnings") for r in ctx.get("recommendations", [])}
    rec_html = ""
    for i, r in enumerate(recs):
        pts = series(db, r["code"])
        chg = (pts[-1][1] / pts[-60][1] - 1) * 100 if len(pts) >= 60 else None
        price, stop, q = r["entry_ref"], r["stop"], r["shares"]
        need = price * q if price and q else None
        risk = (price - stop) * q if price and stop and q else None
        drop = (stop / price - 1) * 100 if price and stop else None
        items = [("살 가격", f"{money(price, us)} 근처"),
                 ("손절가", f'{money(stop, us)} <small class="down">{pct(drop, 1)}</small>'),
                 ("목표가", money(r["target"], us) if r["target"] else "없음 · 오르는 동안 보유"),
                 ("수량", shares(q)),
                 ("필요한 돈", f"{money(need, us)}" + (f" <small>내 돈의 {need / cap:.0%}</small>" if need and cap else "")),
                 ("틀리면 최대", f'<span class="down">{money(-risk if risk else None, us)}</span>'
                  + (f" <small>내 돈의 {risk / cap:.0%}</small>" if risk and cap else ""))]
        if earn.get(r["code"]):
            items.append(("다음 실적 발표", h(earn[r["code"]])))
        flag = (f'<div class="alert">⛔ 아침 브리핑에서 보류 권고: {h(r["flagged"])}</div>' if r["flagged"] else "")
        body = (flag + chart(pts, stop) + kv(items) +
                '<p class="hint">지정가로 사고, 크게 갭상승해서 시작하면 쫓아가지 마세요. 사면 슬랙에 '
                f'<code>buy {h(r["code"])} 가격 수량</code> 으로 기록하세요.</p>')
        sub = h(LB.strat(r["strategy"])) + (' · <em class="warn">보류</em>' if r["flagged"] else "")
        rec_html += row(r["code"], f'{h(r["code"])} <small class="nm">{h(name(r["code"]))}</small>', sub,
                        money(price, us), f'<span class="{tone(chg)}">3개월 {pct(chg)}</span>' if chg is not None else "",
                        body, open_=(i == 0))
    rec_html = rec_html or empty("😴", "내일 살 종목이 없어요. 쉬어 가는 날이에요.")

    # ---- my stocks
    pos_html = ""
    for c, p in pos.items():
        last = p.get("last_close")
        v = p["qty"] * (last or p["avg_price"])
        items = [("수량", shares(p["qty"])), ("평균 산 가격", money(p["avg_price"], us)), ("현재가", money(last, us)),
                 ("손절가", money(p["plan_stop"], us) if p["plan_stop"] else '<em class="warn">미설정 · 꼭 정하세요</em>'),
                 ("목표가", money(p["plan_target"], us)), ("고른 이유", h(LB.strat(p["strategy"]))),
                 ("처음 산 날", h(p["first_buy"]))]
        pos_html += row(c, f'{h(c)} <small class="nm">{h(name(c))}</small>', shares(p["qty"]), money(v, us),
                        f'<span class="{tone(p.get("pnl_pct"))}">{money((last - p["avg_price"]) * p["qty"] if last else None, us, True)}'
                        f' ({pct(p.get("pnl_pct"), 2)})</span>', chart(series(db, c), p["plan_stop"], p["avg_price"]) + kv(items))
    pos_html = pos_html or empty("📭", "보유 종목이 없어요. 사면 슬랙에 buy 티커 가격 수량 으로 기록하세요.")
    trip_html = "".join(
        f'<li><span>{avatar(t["code"])}<span class="rt"><b>{h(t["code"])}</b><small>{h(t["sell_date"])} 매도 · '
        f'{shares(t["qty"])}</small></span></span><b class="{tone(t["ret_pct"])}">{pct(t["ret_pct"], 2)}</b></li>'
        for t in reversed(trips[-15:]))
    if trip_html:
        pos_html += f'<h3>판 기록</h3><ul class="plain">{trip_html}</ul>'

    # ---- reports
    kinds = {"evening": "저녁 리포트", "morning": "아침 브리핑", "weekly": "주간 학습", "ask": "질문 답변"}
    tabs, panes = "", ""
    for k, label in kinds.items():
        r = db.query("SELECT * FROM reports WHERE kind=? ORDER BY id DESC LIMIT 1", (k,))
        if not r:
            continue
        first = not tabs
        tabs += f'<button class="tab{" on" if first else ""}" data-tab="rep-{k}">{label}</button>'
        long_ = len(r[0]["body"]) > 700
        panes += (f'<div class="pane{" on" if first else ""}{" clamp" if long_ else ""}" id="rep-{k}"><small class="when">'
                  f'{h(r[0]["ts"][:16].replace("T", " "))}</small>{report_html(r[0]["body"])}'
                  + ('<button class="more">전체 보기</button>' if long_ else "") + '</div>')
    rep_html = (f'<div class="tabs">{tabs}</div>{panes}' if tabs
                else empty("📰", "아직 리포트가 없어요. 장이 열리는 날 아침·저녁에 생겨요."))

    # ---- strategies
    weights = ctx.get("weights", {})
    strat_html = ""
    strats = db.strategies(("active", "muted", "retired"))
    strats.sort(key=lambda s: -((s["backtest"] or {}).get("full", {}).get("expectancy_pct") or -99))
    for s in strats:
        bt = s["backtest"] or {}
        f, o = bt.get("full", {}), bt.get("oos", {})
        pf, opf = f.get("profit_factor"), o.get("profit_factor")
        if pf and pf >= 1.2 and (opf or 0) >= 1.1:
            verdict = '<em class="good">좋음</em>'
        elif pf and pf >= 1.0:
            verdict = '<em>보통</em>'
        else:
            verdict = '<em class="bad">안 좋음</em>' if pf else ""
        p = db.query("SELECT COUNT(*) n, AVG(ret_pct) a FROM paper_trades WHERE strategy=? AND status='closed'",
                     (s["name"],))[0]
        po = db.query("SELECT COUNT(*) n FROM paper_trades WHERE strategy=? AND status IN ('open','pending')",
                      (s["name"],))[0]["n"]
        items = [("이긴 비율", pct(f.get("win_rate"), 0, False)), ("거래 횟수", f'{f.get("trades", "-")}번'),
                 ("최근 성적", f'<span class="{tone(o.get("expectancy_pct"))}">{pct(o.get("expectancy_pct"), 2)}</span> <small>한 번 평균</small>'),
                 ("최악일 때 하락", f'<span class="down">{pct(-(f.get("mdd_pct") or 0), 0)}</span>'),
                 ("가상 매매", f'{p["n"]}번 끝 · {po}번 진행 중' + (f' · 평균 <span class="{tone(p["a"])}">{pct(p["a"], 2)}</span>' if p["n"] else "")),
                 ("상태", STATUS.get(s["status"], s["status"]) + (f' · 지금 비중 {weights[s["name"]]:.0%}' if weights.get(s["name"]) else ""))]
        body = f'<p class="desc">{h((s["spec"].get("description") or "").strip())}</p>' + kv(items)
        strat_html += row(LB.strat(s["name"])[:2], h(LB.strat(s["name"])), f"{verdict} 이긴 비율 {pct(f.get('win_rate'), 0, False)}",
                          f'<span class="{tone(f.get("expectancy_pct"))}">{pct(f.get("expectancy_pct"), 2)}</span>',
                          "한 번 거래 평균", body)
    strat_html = (f'<p class="lead">과거 5년 데이터로 시험해 본 결과예요. 오른쪽 숫자는 이 방식으로 한 번 사고팔 때 평균 수익이에요.</p>'
                  + strat_html) if strat_html else empty("🧪", "전략이 없어요.")

    # ---- more: paper trades, learning log, proposals, edge map
    paper = db.query("SELECT * FROM paper_trades WHERE status IN ('open','pending') ORDER BY signal_date DESC LIMIT 30")
    closed = db.query("SELECT * FROM paper_trades WHERE status='closed' ORDER BY exit_date DESC LIMIT 30")
    li = lambda code, t, sub, right: (f'<li><span>{avatar(code)}<span class="rt"><b>{t}</b><small>{sub}</small></span></span>'  # noqa: E731
                                      f'{right}</li>')
    paper_html = "".join(li(p["code"], h(p["code"]), f'{h(LB.strat(p["strategy"]))} · {h(p["signal_date"])}',
                            f'<small>{"사기 전" if p["status"] == "pending" else "들고 있음"}</small>') for p in paper)
    paper_html += "".join(li(p["code"], h(p["code"]), f'{h(LB.strat(p["strategy"]))} · {h(p["exit_date"])} 정리',
                             f'<b class="{tone(p["ret_pct"])}">{pct(p["ret_pct"], 2)}</b>') for p in closed)
    paper_html = (f'<ul class="plain">{paper_html}</ul>' if paper_html
                  else empty("🎮", "가상 매매 기록이 아직 없어요. 매일 저녁 자동으로 쌓여요."))
    status_ko = {"open": "확인 중", "confirmed": "맞았음", "rejected": "틀렸음"}
    ins = db.query("SELECT * FROM insights ORDER BY id DESC LIMIT 30")
    ins_html = "".join(f'<li class="note"><em class="{ {"confirmed": "good", "rejected": "bad"}.get(i["status"], "")}">'
                       f'{status_ko.get(i["status"], i["status"])}</em> {h(i["text"])}'
                       + (f'<small>→ {h(i["note"])}</small>' if i["note"] else "") + f'<small>{h(i["ts"][:10])}</small></li>'
                       for i in ins)
    ins_html = f'<ul class="plain">{ins_html}</ul>' if ins else empty("📒", "시스템이 배운 것이 아직 없어요.")
    props = db.query("SELECT * FROM proposals WHERE status='pending' ORDER BY id")
    prop_html = "".join(f'<li class="note"><b>#{p["id"]} {h(p["title"])}</b><small>{h((p["rationale"] or "")[:400])}</small>'
                        f'<small>슬랙에서 <code>approve {p["id"]}</code> 또는 <code>reject {p["id"]}</code></small></li>'
                        for p in props)
    prop_html = f'<ul class="plain">{prop_html}</ul>' if props else empty("✅", "승인할 제안이 없어요.")
    edge = db.get("edge_summary")
    edge_html = report_html(edge) if edge else empty("🗺️", "토요일 주간 학습 뒤에 생겨요.")
    more = "".join(f'<details class="fold"><summary>{t}<i class="chev"></i></summary><div>{c}</div></details>' for t, c in [
        ("가상 매매 <small>돈 없이 매일 따라 해 본 기록</small>", paper_html),
        ("학습 일지 <small>시스템이 배운 것</small>", ins_html),
        (f"승인 대기 제안 <small>{len(props)}개</small>", prop_html),
        ("엣지 맵 <small>어떤 조건에서 벌고 잃는지</small>", edge_html)])

    sections = [
        ("recs", "내일 살 후보", f"{len(recs)}개" if recs else "", rec_html),
        ("mine", "내 주식", f"{len(pos)}종목" if pos else "", pos_html),
        ("reports", "리포트", "", rep_html),
        ("strats", "전략 성적표", f"{len(strats)}개", strat_html),
        ("more", "더 보기", "", more),
    ]
    nav = "".join(f'<a href="#{i}">{t}</a>' for i, t, _, _ in sections)
    body = "".join(f'<section class="card" id="{i}"><h2>{t} <small>{n}</small></h2>{c}</section>' for i, t, n, c in sections)
    return PAGE.replace("{{nav}}", nav).replace("{{hero}}", hero + alerts).replace("{{body}}", body)


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow"><meta name="theme-color" content="#f2f4f6"><title>Stock Mining</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<style>
:root{color-scheme:light;--bg:#f2f4f6;--card:#fff;--line:#eef0f3;--ink:#191f28;--ink2:#4e5968;--ink3:#8b95a1;--fill:#f2f4f6;
--accent:#3182f6;--up:#f04452;--down:#3182f6;--upbg:#fdeef0;--downbg:#e8f3ff;--warn:#c27a00;--warnbg:#fff4e0}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#101013;--card:#1c1c21;--line:#2a2a31;--ink:#f2f3f5;--ink2:#b0b8c1;
--ink3:#7e8792;--fill:#2a2a31;--accent:#4c94ff;--up:#ff5c6a;--down:#4c94ff;--upbg:#3a1f24;--downbg:#1a2a44;--warn:#ffb340;--warnbg:#3a2a10}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Pretendard Variable",Pretendard,-apple-system,BlinkMacSystemFont,
"Apple SD Gothic Neo","Noto Sans KR",sans-serif;-webkit-font-smoothing:antialiased;letter-spacing:-.01em}
.wrap{max-width:600px;margin:0 auto;padding:0 12px 48px}
nav{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(10px)}
nav div{max-width:600px;margin:0 auto;padding:10px 12px;display:flex;gap:6px;overflow-x:auto;scrollbar-width:none}
nav div::-webkit-scrollbar{display:none}
nav a{white-space:nowrap;color:var(--ink2);text-decoration:none;padding:7px 13px;border-radius:999px;font-size:14px;font-weight:600;background:var(--card)}
nav a.on{background:var(--ink);color:var(--card)}
.card,.hero{background:var(--card);border-radius:20px;padding:22px 20px;margin:12px 0;scroll-margin-top:60px}
.hero{padding-top:26px}.hero small{color:var(--ink3);font-size:13px}.hero>span{display:block;margin-top:14px;color:var(--ink2);font-size:15px}
.big{display:block;font-size:34px;font-weight:700;letter-spacing:-.02em;margin:2px 0 4px;font-variant-numeric:tabular-nums}
.hero p{margin:0;color:var(--ink2);font-size:15px;font-weight:600}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:16px}.chips span{background:var(--fill);border-radius:10px;padding:6px 10px;font-size:13px;color:var(--ink2)}
.chips b{color:var(--ink);font-weight:700}
h2{font-size:20px;font-weight:700;margin:0 0 10px}h2 small{color:var(--accent);font-size:15px;font-weight:600;margin-left:4px}
h3{font-size:15px;color:var(--ink2);margin:22px 0 4px}
.up{color:var(--up)!important}.down{color:var(--down)!important}
.lead{color:var(--ink3);font-size:14px;margin:-4px 0 6px}
.row{border-radius:14px;margin:0 -8px}.row[open]{background:var(--fill)}
.row summary{list-style:none;display:flex;align-items:center;gap:12px;padding:12px 8px;cursor:pointer;border-radius:14px}
.row summary::-webkit-details-marker,.fold summary::-webkit-details-marker{display:none}
.row summary:hover{background:var(--fill)}
.av{flex:0 0 42px;height:42px;border-radius:50%;background:var(--fill);display:grid;place-items:center;font-size:11.5px;font-weight:700;
color:var(--ink2);letter-spacing:-.03em;overflow:hidden}
.row[open] .av{background:var(--card)}
.rt{flex:1;min-width:0;display:flex;flex-direction:column}.rt b{font-size:16px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rt small,.rr small{color:var(--ink3);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nm{color:var(--ink3);font-weight:400;font-size:13px;margin-left:2px}
.rr{display:flex;flex-direction:column;align-items:flex-end;font-variant-numeric:tabular-nums}.rr b{font-size:16px;font-weight:600}
.chev{width:8px;height:8px;border-right:2px solid var(--ink3);border-bottom:2px solid var(--ink3);transform:rotate(45deg);margin:0 4px 4px 2px;
transition:transform .2s}details[open]>summary .chev{transform:rotate(-135deg);margin-bottom:-4px}
.rb{padding:2px 12px 16px}
em{font-style:normal;font-size:12px;font-weight:600;padding:2px 7px;border-radius:6px;background:var(--fill);color:var(--ink2);margin-right:4px}
.row[open] em{background:var(--card)}em.good{background:var(--upbg);color:var(--up)}em.bad{background:var(--downbg);color:var(--down)}
em.warn{background:var(--warnbg);color:var(--warn)}
.kv{margin:8px 0 0;display:grid;gap:0}.kv div{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid var(--line)}
.kv div:last-child{border:0}.kv dt{color:var(--ink2)}.kv dd{margin:0;font-weight:600;text-align:right;font-variant-numeric:tabular-nums}
.kv small{color:var(--ink3);font-weight:400;font-size:13px}
.row[open] .kv div{border-color:color-mix(in srgb,var(--ink3) 18%,transparent)}
.hint,.desc{color:var(--ink2);font-size:14px;margin:12px 0 0}.desc{margin:0 0 4px}
code{background:var(--card);padding:1px 6px;border-radius:6px;font-size:13px;white-space:nowrap}
.clamp .report{max-height:340px;overflow:hidden;-webkit-mask-image:linear-gradient(#000 70%,transparent)}
.more{display:block;width:100%;margin-top:8px;border:0;background:var(--fill);color:var(--ink2);font:inherit;font-weight:600;
padding:12px;border-radius:12px;cursor:pointer}
.chart{position:relative;height:170px;margin:4px 0 6px;touch-action:pan-y}.chart svg{display:block;width:100%;height:100%;overflow:visible}
.chart .ln{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.chart .ref{stroke:var(--ink3);stroke-width:1;stroke-dasharray:4 4}.chart .reflbl{fill:var(--ink3);font-size:11px}
.chart .cross{stroke:var(--ink3);stroke-width:1}.chart .dot{stroke:var(--card);stroke-width:2}
.tip{position:absolute;top:-6px;transform:translateX(-50%);background:var(--ink);color:var(--card);font-size:12px;font-weight:600;
padding:4px 8px;border-radius:8px;white-space:nowrap;pointer-events:none;font-variant-numeric:tabular-nums}
.empty{display:flex;flex-direction:column;align-items:center;gap:6px;padding:22px 8px;color:var(--ink3);text-align:center;font-size:14px}
.empty span{font-size:30px}
.alert{background:var(--warnbg);color:var(--warn);border-radius:14px;padding:12px 14px;margin:10px 0;font-size:14px;font-weight:500}
.tabs{display:flex;gap:6px;overflow-x:auto;margin:0 0 10px;scrollbar-width:none}
.tab{border:0;background:var(--fill);color:var(--ink2);font:inherit;font-size:14px;font-weight:600;padding:7px 13px;border-radius:999px;cursor:pointer;white-space:nowrap}
.tab.on{background:var(--ink);color:var(--card)}.pane{display:none}.pane.on{display:block}
.when{color:var(--ink3);font-size:13px}
.report{white-space:pre-wrap;word-break:keep-all;overflow-wrap:anywhere;font-size:15px;line-height:1.7;margin-top:6px;color:var(--ink)}
.report b{font-weight:700}.report a{color:var(--accent)}.report .li{display:block;padding-left:14px;text-indent:-10px}
.report .li::before{content:"·";color:var(--ink3);margin-right:6px}
.plain{list-style:none;margin:0;padding:0}.plain li{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 0}
.plain li>span{display:flex;align-items:center;gap:12px;min-width:0}.plain li b{font-variant-numeric:tabular-nums}
.plain li.note{display:block;border-bottom:1px solid var(--line);padding:12px 0}.plain li.note:last-child{border:0}
.plain li.note small{display:block;color:var(--ink3);font-size:13px;margin-top:3px}
.fold{border-top:1px solid var(--line)}.fold:first-of-type{border:0}
.fold summary{list-style:none;display:flex;align-items:center;padding:14px 0;font-weight:600;cursor:pointer;font-size:16px}
.fold summary small{color:var(--ink3);font-weight:400;font-size:13px;margin-left:8px}.fold summary .chev{margin-left:auto}
footer{text-align:center;color:var(--ink3);font-size:12px;padding:8px 16px 32px}
</style></head><body>
<nav><div>{{nav}}</div></nav>
<div class="wrap">{{hero}}{{body}}</div>
<footer>보기 전용 · 기록과 승인은 슬랙에서 · 투자 판단과 주문은 직접 하세요</footer>
<script>
(function(){
var NS="http://www.w3.org/2000/svg";
function el(t,a){var e=document.createElementNS(NS,t);for(var k in a)e.setAttribute(k,a[k]);return e}
function fmt(v){return "$"+v.toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2})}
function draw(box){
  if(box.dataset.done||!box.offsetWidth)return;box.dataset.done=1;
  var D=JSON.parse(box.dataset.chart),c=D.c,W=box.offsetWidth,H=box.offsetHeight,pad=6,lab=64;
  var vals=c.slice();if(D.stop)vals.push(D.stop);if(D.avg)vals.push(D.avg);
  var lo=Math.min.apply(null,vals),hi=Math.max.apply(null,vals),sp=(hi-lo)||1;lo-=sp*.06;hi+=sp*.06;sp=hi-lo;
  var X=function(i){return pad+i/(c.length-1)*(W-pad*2-lab)},Y=function(v){return pad+(hi-v)/sp*(H-pad*2)};
  var col=getComputedStyle(document.documentElement).getPropertyValue(c[c.length-1]>=c[0]?"--up":"--down").trim();
  var svg=el("svg",{viewBox:"0 0 "+W+" "+H,role:"img","aria-label":"최근 "+c.length+"일 종가, 마지막 "+fmt(c[c.length-1])});
  [["stop","손절 "],["avg","평단 "]].forEach(function(r){var v=D[r[0]];if(!v)return;
    svg.appendChild(el("line",{x1:pad,x2:W-pad-lab+4,y1:Y(v),y2:Y(v),"class":"ref"}));
    var t=el("text",{x:W-pad-lab+8,y:Y(v)+4,"class":"reflbl"});t.textContent=r[1]+fmt(v);svg.appendChild(t)});
  var d=c.map(function(v,i){return(i?"L":"M")+X(i).toFixed(1)+" "+Y(v).toFixed(1)}).join("");
  svg.appendChild(el("path",{d:d,"class":"ln",stroke:col}));
  var lx=X(c.length-1),ly=Y(c[c.length-1]);
  var end=el("circle",{cx:lx,cy:ly,r:4,fill:col,"class":"dot"});svg.appendChild(end);
  var cross=el("line",{y1:0,y2:H,"class":"cross",visibility:"hidden"}),dot=el("circle",{r:5,fill:col,"class":"dot",visibility:"hidden"});
  svg.appendChild(cross);svg.appendChild(dot);box.appendChild(svg);
  var tip=document.createElement("div");tip.className="tip";tip.style.display="none";box.appendChild(tip);
  function show(e){var r=box.getBoundingClientRect(),x=e.clientX-r.left;
    var i=Math.round((x-pad)/(W-pad*2-lab)*(c.length-1));i=Math.max(0,Math.min(c.length-1,i));
    var px=X(i);cross.setAttribute("x1",px);cross.setAttribute("x2",px);dot.setAttribute("cx",px);dot.setAttribute("cy",Y(c[i]));
    cross.setAttribute("visibility","visible");dot.setAttribute("visibility","visible");
    var p=D.d[i].slice(5).replace("-","/");tip.textContent=p+"  "+fmt(c[i]);tip.style.display="block";
    tip.style.left=Math.max(50,Math.min(W-50,px))+"px"}
  function hide(){cross.setAttribute("visibility","hidden");dot.setAttribute("visibility","hidden");tip.style.display="none"}
  box.addEventListener("pointermove",show);box.addEventListener("pointerdown",show);box.addEventListener("pointerleave",hide);
}
function all(){document.querySelectorAll(".chart").forEach(draw)}
document.querySelectorAll("details").forEach(function(d){d.addEventListener("toggle",all)});
all();
document.querySelectorAll(".tab").forEach(function(b){b.addEventListener("click",function(){
  document.querySelectorAll(".tab,.pane").forEach(function(x){x.classList.remove("on")});
  b.classList.add("on");document.getElementById(b.dataset.tab).classList.add("on")})});
document.querySelectorAll(".more").forEach(function(b){b.addEventListener("click",function(){
  b.parentNode.classList.remove("clamp");b.remove()})});
var links=[].slice.call(document.querySelectorAll("nav a"));
function spy(){var y=window.scrollY+90,cur=links[0];links.forEach(function(a){var s=document.querySelector(a.getAttribute("href"));
  if(s&&s.offsetTop<=y)cur=a});links.forEach(function(a){a.classList.toggle("on",a===cur)})}
window.addEventListener("scroll",spy,{passive:true});spy();
})();
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    cfg: Config
    user = "me"
    password: str | None = None

    def log_message(self, fmt, *args):  # quiet; errors still go to the log below
        pass

    def _authorized(self) -> bool:
        if not self.password:
            return True
        got = self.headers.get("Authorization", "")
        want = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        return hmac.compare_digest(got.encode(), want.encode())

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="stockmining", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.split("?")[0] != "/":
            return self._send(404, "not found", "text/plain")
        try:
            db = ReadOnlyDB(self.cfg.path("db_path"))
            try:
                html = render(self.cfg, db)
            finally:
                db.conn.close()
            self._send(200, html, "text/html; charset=utf-8")
        except Exception:
            log.exception("render failed")
            self._send(500, "대시보드를 그리지 못했어요. 서버 로그를 확인하세요.", "text/plain; charset=utf-8")

    def _send(self, code: int, text: str, ctype: str):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.end_headers()
        self.wfile.write(data)


def serve(cfg: Config, host: str, port: int) -> None:
    pw = os.environ.get("WEB_PASSWORD")
    if not pw and host not in ("127.0.0.1", "localhost"):
        raise SystemExit("WEB_PASSWORD 가 없으면 localhost 에서만 띄울 수 있어요")
    Handler.cfg, Handler.password, Handler.user = cfg, pw, os.environ.get("WEB_USER", "me")
    log.info("dashboard on http://%s:%s", host, port)
    ThreadingHTTPServer((host, port), Handler).serve_forever()

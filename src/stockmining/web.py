"""Read-only web dashboard: ``stockmining web`` — a small multi-page site (stdlib HTTP server).

Pages: 홈 · 내 자산 · 추천 · 전략(목록·상세) · 모의투자(타임머신 리플레이) · 종목 상세 · 리포트(목록·상세·학습 일지·제안·엣지 맵).
Prices on screen are live (Naver for KRX, Yahoo for US/FX; see quotes.py) and refresh every 20 s through
``/api/quotes``. The DB is opened read-only. Access needs HTTP basic auth with WEB_PASSWORD (user WEB_USER,
default "me"); without a password the server only listens on localhost.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import docfmt as DF
from . import journal as J
from . import labels as LB
from . import quotes as Q
from . import live as LV
from . import news as NW
from . import websim as WS
from .config import Config
from .db import DB
from .webui import (avatar, card, chart, chips, doc_html, empty, explain, explain_exit, fold_row, h, inline, kst, kv,
                    link_row, live, market_state, money, news_list, page, pct, report_doc, shares, tone)

log = logging.getLogger(__name__)
STATUS = {"active": "사용 중", "muted": "쉬는 중", "retired": "퇴역"}
KIND = {"playbook": ("📓", "투자 노트"), "evening": ("📊", "저녁 리포트"), "morning": ("☀️", "아침 브리핑"), "weekly": ("🧠", "주간 학습"),
        "ask": ("💬", "질문 답변"), "weekly_research": ("🔎", "주간 리서치")}


class ReadOnlyDB(DB):
    def __init__(self, path: str | Path):  # skip schema/WAL setup: never write from the web process
        self.path = Path(path)
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
        self.conn.row_factory = sqlite3.Row


class NotFound(Exception):
    pass


class Ctx:
    def __init__(self, cfg: Config, db: DB, hdb: DB | None = None, markets: list | None = None):
        """``hdb``: DB holding 내 자산 (the main instance); ``markets``: every (cfg, db) whose swing positions count."""
        self.cfg, self.db = cfg, db
        self.hdb = hdb or db
        self.markets = markets or [(cfg, db)]
        self.cur = "USD" if cfg.is_us else "KRW"
        self.info = db.ticker_info()
        self._ctx = None

    def name(self, code: str) -> str:
        return self.info.get(code, {}).get("name") or code

    @property
    def evening(self) -> dict:
        if self._ctx is None:
            self._ctx = json.loads(self.db.get("last_evening_ctx") or "{}")
        return self._ctx

    def series(self, code: str, n: int = 250) -> list[tuple[str, float]]:
        if Q.is_kr(code) and self.cfg.is_us:
            return Q.history_kr(code, n)
        rows = self.db.query("SELECT date, close FROM prices WHERE code=? AND close IS NOT NULL ORDER BY date DESC LIMIT ?",
                             (code, n))
        if not rows and Q.is_kr(code):  # e.g. a KR ETF in 내 자산 (ETFs are not in the stock universe)
            return Q.history_kr(code, n)
        return [(r["date"], r["close"]) for r in reversed(rows)]

    def holdings(self) -> list[dict]:
        """Long-term holdings (holdings table) + open swing positions (journal), each with currency."""
        try:
            rows = self.hdb.query("SELECT * FROM holdings ORDER BY market, code")
        except sqlite3.OperationalError:  # table not created yet
            rows = []
        out = [dict(r, cur="KRW" if r["market"] == "kr" else "USD", kind="long") for r in rows]
        for cfg, db in self.markets:
            pos, _ = J.positions_and_roundtrips(db)
            info = db.ticker_info() if db is not self.db else self.info
            for c, p in pos.items():
                out.append({"code": c, "market": "us" if cfg.is_us else "kr", "name": info.get(c, {}).get("name") or c,
                            "qty": p["qty"], "avg_price": p["avg_price"], "cur": "USD" if cfg.is_us else "KRW",
                            "kind": "swing", "pos": p})
        return out

    def recs(self) -> tuple[str | None, list[dict]]:
        d = self.db.query("SELECT MAX(date) d FROM recommendations")[0]["d"]
        return d, (self.db.query("SELECT * FROM recommendations WHERE date=? ORDER BY combined_score DESC", (d,)) if d else [])


def verdict(bt: dict) -> str:
    pf, opf = bt.get("full", {}).get("profit_factor"), bt.get("oos", {}).get("profit_factor")
    if pf and pf >= 1.2 and (opf or 0) >= 1.1:
        return '<em class="good">좋음</em>'
    if pf and pf >= 1.0:
        return "<em>보통</em>"
    return '<em class="bad">안 좋음</em>' if pf else ""


def asset_av(code: str, name: str | None) -> str:
    n = name or code
    if re.match(r"(KODEX|TIGER|ACE|RISE|SOL|KBSTAR|HANARO|ARIRANG|PLUS)\b", n):
        return avatar("ETF", "etf")
    return avatar(code)


def _totals(hold: list[dict], qs: dict, fx: float) -> tuple[float, float, float, str]:
    """(value, cost, today's change) in KRW, plus hidden markers the page script uses to keep totals live."""
    val = cost = day = 0.0
    marks = ""
    for x in hold:
        q = qs.get(x["code"]) or {}
        m = fx if x["cur"] == "USD" else 1
        last = q.get("price") or x["avg_price"]
        val += last * x["qty"] * m
        cost += x["avg_price"] * x["qty"] * m
        day += (q.get("change") or 0) * x["qty"] * m
        marks += (f'<i data-hold="{h(x["code"])}" data-cur="{x["cur"]}" data-qty="{x["qty"]}" '
                  f'data-avg="{x["avg_price"]}" data-last="{last}"></i>')
    return val, cost, day, marks


# ---------------------------------------------------------------------------- pages
def home(c: Ctx) -> tuple[str, str, list[str]]:
    hold = c.holdings()
    rdate, recs = c.recs()
    idxs = Q.INDICES if c.cfg.is_us else Q.INDICES_KR
    codes = [x["code"] for x in hold] + [r["code"] for r in recs] + list(idxs)
    qs = Q.get(codes)
    fx = (qs.get("KRW=X") or {}).get("price") or 1350.0
    body = []

    if hold:
        val, cost, day, marks = _totals(hold, qs, fx)
        pnl = val - cost
        body.append(f'<section class="hero"><small>실시간 · 원화 환산</small><span class="lbl">내 자산</span>'
                    f'<b class="big" data-total data-fx="{fx}">{money(val, "KRW")}</b>'
                    f'<p>총 손익 <span class="{tone(pnl)}" data-total-pnl>{money(pnl, "KRW", True)} ({pct(pnl / cost * 100 if cost else 0)})</span>'
                    f' · 오늘 <span class="{tone(day)}" data-total-day>{money(day, "KRW", True)}</span></p>{marks}</section>')
    else:
        body.append('<section class="hero"><span class="lbl">내 자산</span><b class="big">아직 없어요</b>'
                    '<p>슬랙에서 <code>hold 코드 수량 평단</code> 으로 등록하세요</p></section>')

    mk = ""
    for code, label in idxs.items():
        q = qs.get(code) or {}
        price = "-" if q.get("price") is None else (f'{q["price"]:,.1f}원' if code == "KRW=X" else f'{q["price"]:,.2f}')
        mk += (f'<a><span>{label}</span><b>{price}</b>'
               f'<small class="{tone(q.get("change"))}">{pct(q.get("change_pct"))}</small></a>')
    body.append(f'<div class="mkt">{mk}</div>')

    if hold:
        lst = ""
        for x in hold[:4]:
            q = qs.get(x["code"])
            lst += link_row(f"/stock/{x['code']}", asset_av(x["code"], x["name"]), h(x["name"] or x["code"]),
                            shares(x["qty"]), live(x["code"], "val", q, x["cur"], x["qty"]),
                            live(x["code"], "pnl", q, x["cur"], x["qty"], x["avg_price"]))
        body.append(card("내 자산", lst, ("/assets", "전체")))

    lst = ""
    for r in recs:
        q = qs.get(r["code"])
        lst += link_row(f"/stock/{r['code']}", avatar(r["code"]), f'{h(r["code"])} <small class="nm">{h(c.name(r["code"]))}</small>',
                        h(LB.strat(r["strategy"])) + (' · <em class="warn">보류</em>' if r["flagged"] else ""),
                        live(r["code"], "price", q, c.cur), "기준가 대비 " + live(r["code"], "vs", q, c.cur, ref=r["entry_ref"]))
    body.append(card("내일 살 후보", lst or empty("😴", "내일 살 종목이 없어요."), ("/picks", "자세히"),
                     f"{len(recs)}개" if recs else ""))

    mk = NW.recent(c.db, "", 30)
    if mk:
        mood = (c.evening.get("news") or {}).get("market_mood") or {}
        lead = (f'<p class="lead">어제 저녁 기준 시장 뉴스 분위기: <b>{h(NW.LABEL_KO.get(mood.get("label"), "-"))}</b>'
                + (f' (평균 {mood["sent"]:+.1f}, −2~+2)' if mood.get("sent") is not None else "") + '</p>') if mood else ""
        body.append(card("📰 시장 뉴스", lead + news_list(mk, 6), ("/news", "더 보기")))
    body.append(WS.live_home(LV.load(c.db)))
    body.append(WS.home_card(WS.load(c.db)))

    r = c.db.query("SELECT * FROM reports WHERE kind IN ('evening','morning') ORDER BY id DESC LIMIT 1")
    if r:
        d = report_doc(r[0])["doc"]
        icon, label = KIND.get(r[0]["kind"], ("📄", r[0]["kind"]))
        head = d.get("headline") or next((s["title"] for s in d.get("sections", []) if s.get("title")), "")
        body.append(card(f"{icon} {label}", f'<a href="/report/{r[0]["id"]}"><div class="callout">{inline(head)}</div></a>'
                         f'<p class="lead" style="margin-top:8px">{h(kst(r[0]["ts"]))} 작성</p>', ("/reports", "전체")))
    return "Stock Mining", "".join(body), codes


def assets(c: Ctx) -> tuple[str, str, list[str]]:
    hold = c.holdings()
    codes = [x["code"] for x in hold] + ["KRW=X"]
    qs = Q.get(codes)
    fx = (qs.get("KRW=X") or {}).get("price") or 1350.0
    val, cost, day, marks = _totals(hold, qs, fx)
    pnl = val - cost
    out = [f'<section class="hero"><small>실시간 · 달러는 환율 {fx:,.1f}원으로 환산</small><span class="lbl">총 평가금액</span>'
           f'<b class="big" data-total data-fx="{fx}">{money(val, "KRW")}</b>'
           f'<div class="grid2" style="margin-top:12px"><div class="stat"><span>총 손익</span><b class="{tone(pnl)}" data-total-pnl>'
           f'{money(pnl, "KRW", True)} ({pct(pnl / cost * 100 if cost else 0)})</b></div>'
           f'<div class="stat"><span>오늘 변동</span><b class="{tone(day)}" data-total-day>{money(day, "KRW", True)}</b></div>'
           f'<div class="stat"><span>산 금액</span><b>{money(cost, "KRW")}</b></div>'
           f'<div class="stat"><span>종목 수</span><b>{len(hold)}개</b></div></div>{marks}</section>']
    groups: dict[str, list[dict]] = {"국내 장기 보유": [], "해외 장기 보유": [], "스윙 매매": []}
    for x in hold:
        key = "스윙 매매" if x["kind"] == "swing" else ("국내 장기 보유" if x["market"] == "kr" else "해외 장기 보유")
        groups[key].append(x)
    for title, items in groups.items():
        if not items:
            continue
        lst = ""
        for x in items:
            q = qs.get(x["code"])
            sub = shares(x["qty"]) + " · 평단 " + money(x["avg_price"], x["cur"])
            lst += link_row(f"/stock/{x['code']}", asset_av(x["code"], x["name"]), h(x["name"] or x["code"]), sub,
                            live(x["code"], "val", q, x["cur"], x["qty"]), live(x["code"], "pnl", q, x["cur"], x["qty"], x["avg_price"]))
        out.append(card(title, lst, count=f"{len(items)}개"))
    if not hold:
        out.append(card("내 자산", empty("📭", "등록된 자산이 없어요.")))
    out.append('<p class="hint" style="text-align:center">추가·수정은 슬랙에서 <code>hold 코드 수량 평단</code> · 삭제 <code>hold rm 코드</code><br>'
               '스윙 매매는 <code>buy</code>/<code>sell</code> 기록에서 자동으로 들어와요</p>')
    return "내 자산", "".join(out), codes


def picks(c: Ctx) -> tuple[str, str, list[str]]:
    rdate, recs = c.recs()
    qs = Q.get([r["code"] for r in recs])
    cap = c.cfg["account"].get("capital") or 0
    earn = {r["code"]: r.get("next_earnings") for r in c.evening.get("recommendations", [])}
    news_of = {r["code"]: r.get("news") for r in c.evening.get("recommendations", [])}
    lst = ""
    for i, r in enumerate(recs):
        q = qs.get(r["code"])
        price, stop, qty = r["entry_ref"], r["stop"], r["shares"]
        need = price * qty if price and qty else None
        risk = (price - stop) * qty if price and stop and qty else None
        items = [("지금 가격", live(r["code"], "price", q, c.cur) + " " + market_state(q)),
                 ("기준가 대비", live(r["code"], "vs", q, c.cur, ref=price)),
                 ("살 가격", f"{money(price, c.cur)} 근처"),
                 ("손절가", f'{money(stop, c.cur)} <small class="down">{pct((stop / price - 1) * 100 if price and stop else None, 1)}</small>'),
                 ("목표가", money(r["target"], c.cur) if r["target"] else "없음 · 오르는 동안 보유"),
                 ("수량", shares(qty)),
                 ("필요한 돈", money(need, c.cur) + (f" <small>내 돈의 {need / cap:.0%}</small>" if need and cap else "")),
                 ("틀리면 최대", f'<span class="down">{money(-risk if risk else None, c.cur)}</span>'
                  + (f" <small>내 돈의 {risk / cap:.0%}</small>" if risk and cap else ""))]
        if earn.get(r["code"]):
            items.append(("다음 실적 발표", h(earn[r["code"]])))
        nw = news_of.get(r["code"]) or {}
        if nw.get("label") == "risk":
            items.append(("뉴스", '<span class="down">⚠️ ' + h(", ".join(NW.TAG_KO.get(t, t) for t in nw["tags"] if t in NW.NEG_TAGS))
                          + " 소식 · 확인 전엔 사지 마세요</span>"))
        elif nw.get("n"):
            items.append(("뉴스", f'최근 3일 {nw["n"]}건 · {NW.LABEL_KO.get(nw["label"], "")}'))
        flag = f'<div class="alert">⛔ 아침 브리핑 보류 권고: {h(r["flagged"])}</div>' if r["flagged"] else ""
        body = (flag + chart(c.series(r["code"], 120), c.cur, {"stop": stop, "entry": price}) + kv(items)
                + f'<p class="hint">지정가로 사고, 크게 갭상승하면 쫓아가지 마세요. 사면 슬랙에 <code>buy {h(r["code"])} 가격 수량</code>'
                  f'<br><a href="/strategy/{h(r["strategy"])}"><b>이 전략 보기 ›</b></a> · <a href="/stock/{h(r["code"])}"><b>종목 보기 ›</b></a></p>')
        lst += fold_row(avatar(r["code"]), f'{h(r["code"])} <small class="nm">{h(c.name(r["code"]))}</small>',
                        h(LB.strat(r["strategy"])) + (' · <em class="warn">보류</em>' if r["flagged"] else ""),
                        live(r["code"], "price", q, c.cur), "기준가 대비 " + live(r["code"], "vs", q, c.cur, ref=price), body, i == 0)
    out = [card("내일 살 후보", lst or empty("😴", "내일 살 종목이 없어요. 쉬어 가는 날이에요."), count=f"{len(recs)}개" if recs else "")]
    skipped = c.evening.get("skipped_for_earnings") or []
    if skipped:
        out.append(card("🚫 뺀 종목", "".join(
            link_row(f"/stock/{s['code']}", avatar(s["code"]), h(s["code"]), f"실적 발표 {h(s['next_earnings'])} 임박", "")
            for s in skipped)))
    if rdate:
        out.append(f'<p class="updated">{h(rdate)} 장 마감 기준으로 고른 후보예요</p>')
    return "추천", "".join(out), [r["code"] for r in recs]


def strategies(c: Ctx) -> tuple[str, str, list[str]]:
    strats = c.db.strategies(("active", "muted", "retired"))
    strats.sort(key=lambda s: -((s["backtest"] or {}).get("full", {}).get("expectancy_pct") or -99))
    weights = c.evening.get("weights", {})
    lst = ""
    for s in strats:
        bt = s["backtest"] or {}
        f = bt.get("full", {})
        p = c.db.query("SELECT COUNT(*) n FROM paper_trades WHERE strategy=?", (s["name"],))[0]["n"]
        sub = f"{verdict(bt)} 이긴 비율 {pct(f.get('win_rate'), 0, False)} · 가상 매매 {p}번"
        if weights.get(s["name"]):
            sub += f" · 비중 {weights[s['name']]:.0%}"
        lst += link_row(f"/strategy/{s['name']}", avatar(LB.strat(s["name"])[:2]),
                        h(LB.strat(s["name"])) + ("" if s["status"] == "active" else f' <em>{STATUS.get(s["status"])}</em>'),
                        sub, f'<span class="{tone(f.get("expectancy_pct"))}">{pct(f.get("expectancy_pct"))}</span>', "한 번 거래 평균")
    body = card("전략 성적표", '<p class="lead">과거 5년 데이터로 시험해 본 결과예요. 누르면 어떤 전략인지 볼 수 있어요.</p>' + lst,
                count=f"{len(strats)}개")
    return "전략", body, []


def strategy(c: Ctx, name: str) -> tuple[str, str, list[str]]:
    rows = [s for s in c.db.strategies(("active", "muted", "retired")) if s["name"] == name]
    if not rows:
        raise NotFound
    s = rows[0]
    from . import rules as R
    spec = R.resolve(s["spec"])
    bt = s["backtest"] or {}
    f, o = bt.get("full", {}), bt.get("oos", {})
    out = [f'<section class="hero"><small>{h(name)} · {h(STATUS.get(s["status"], s["status"]))}</small>'
           f'<span class="lbl">{verdict(bt)}</span><b class="big" style="font-size:28px">{h(LB.strat(name))}</b>'
           f'<p class="desc">{h((s["spec"].get("description") or "").strip())}</p>'
           + (f'<p class="lead" style="margin-top:8px">출처: {h(s["spec"].get("source_note"))}</p>' if s["spec"].get("source_note") else "")
           + "</section>"]

    ent = spec["entry"]
    conds = "".join(f"<li>{h(explain(e))}<code>{h(e)}</code></li>" for e in ent["all"])
    if ent.get("any"):
        conds += "<li>다음 중 하나라도: " + " / ".join(h(explain(e)) for e in ent["any"]) + "</li>"
    regimes = spec.get("regimes") or ["bull", "neutral", "bear"]
    how = ("조건이 모두 맞은 다음 날 시가에 산다" if ent.get("type", "next_open") == "next_open"
           else f"다음 날 시가 + 전날 변동폭×{ent.get('k')}를 넘으면 산다")
    rank = spec.get("rank")
    rank_txt = (f" 후보가 많으면 {h(explain(rank.lstrip('-')))}이(가) {'작은' if rank.startswith('-') else '큰'} 순서로 고른다."
                if rank else "")
    out.append(card("언제 사나요", f'<ol class="steps">{conds}</ol><p class="hint">{how}. '
                    f'시장이 {"·".join(LB.regime(r) for r in regimes)}일 때만 산다.{rank_txt}</p>'))
    out.append(card("언제 파나요", '<ol class="steps">' + "".join(f"<li>{h(x)}</li>" for x in explain_exit(spec["exit"])) + "</ol>"))

    def stat(label, a, b, fmt):
        return f"<tr><td>{label}</td><td>{fmt(a)}</td><td>{fmt(b)}</td></tr>"
    signed = lambda v: f'<span class="{tone(v)}">{pct(v)}</span>'  # noqa: E731
    tbl = ('<table class="t"><tr><th></th><th>전체 기간</th><th>최근 구간</th></tr>'
           + stat("거래 횟수", f.get("trades"), o.get("trades"), lambda v: f"{v}번" if v is not None else "-")
           + stat("이긴 비율", f.get("win_rate"), o.get("win_rate"), lambda v: pct(v, 0, False))
           + stat("한 번 거래 평균", f.get("expectancy_pct"), o.get("expectancy_pct"), signed)
           + stat("번 돈 ÷ 잃은 돈", f.get("profit_factor"), o.get("profit_factor"), lambda v: "-" if v is None else f"{v:.2f}")
           + stat("1년 수익률", f.get("cagr_pct"), o.get("cagr_pct"), signed)
           + stat("최악일 때 하락", f.get("mdd_pct"), o.get("mdd_pct"), lambda v: "-" if v is None else f'<span class="down">-{v:.0f}%</span>')
           + stat("평균 보유", f.get("avg_hold"), o.get("avg_hold"), lambda v: "-" if v is None else f"{v:.0f}일")
           + "</table>")
    out.append(card("과거 성적", tbl + (f'<p class="lead" style="margin-top:8px">최근 구간 = {h(bt.get("split"))} 이후 '
                                       '(규칙을 고를 때 안 본 기간). 번 돈 ÷ 잃은 돈이 1보다 크면 버는 전략이에요.</p>'
                                       if bt.get("split") else "")))
    reg = f.get("by_regime") or {}
    if reg:
        order = {"bull": 0, "neutral": 1, "bear": 2}
        rr = "".join(f'<tr><td>{h(LB.regime(k))}</td><td>{v["trades"]}번</td><td>{pct(v["win_rate"], 0, False)}</td>'
                     f'<td class="{tone(v["expectancy_pct"])}">{pct(v["expectancy_pct"])}</td></tr>'
                     for k, v in sorted(reg.items(), key=lambda kv_: order.get(kv_[0], 9)))
        out.append(card("시장 분위기별", f'<table class="t"><tr><th></th><th>거래</th><th>이긴 비율</th><th>한 번 평균</th></tr>{rr}</table>'))

    sd = c.db.query("SELECT MAX(date) d FROM signals WHERE strategy=?", (name,))[0]["d"]
    sigs = c.db.query("SELECT * FROM signals WHERE strategy=? AND date=? ORDER BY score DESC LIMIT 15", (name, sd)) if sd else []
    paper = c.db.query("SELECT * FROM paper_trades WHERE strategy=? "
                       "ORDER BY COALESCE(exit_date, entry_date, signal_date) DESC LIMIT 20", (name,))
    codes = [x["code"] for x in sigs] + [p["code"] for p in paper if p["status"] in ("open", "pending")]
    qs = Q.get(codes)
    lst = "".join(link_row(f"/stock/{x['code']}", avatar(x["code"]), f'{h(x["code"])} <small class="nm">{h(c.name(x["code"]))}</small>',
                           f"신호 때 {money(x['close'], c.cur)} · 손절 {money(x['stop'], c.cur)}",
                           live(x["code"], "price", qs.get(x["code"]), c.cur),
                           "신호 대비 " + live(x["code"], "vs", qs.get(x["code"]), c.cur, ref=x["close"])) for x in sigs)
    out.append(card("최근 신호", lst or empty("🔕", "최근에 신호가 나온 종목이 없어요."), count=h(sd or "")))
    pl = ""
    for p in paper:
        if p["status"] == "closed":
            right, rs = f'<span class="{tone(p["ret_pct"])}">{pct(p["ret_pct"])}</span>', f"{h(p['exit_date'])} 정리"
        else:
            right = live(p["code"], "vs", qs.get(p["code"]), c.cur, ref=p["entry_price"]) if p["entry_price"] else "사기 전"
            rs = "들고 있음" if p["status"] == "open" else "다음 날 시가에 삼"
        pl += link_row(f"/stock/{p['code']}", avatar(p["code"]), h(p["code"]), f"{h(p['signal_date'])} 신호", right, rs)
    out.append(card("가상 매매", pl or empty("🎮", "아직 기록이 없어요. 매일 저녁 자동으로 쌓여요.")))
    return LB.strat(name), "".join(out), codes


def stock(c: Ctx, code: str) -> tuple[str, str, list[str]]:
    code = code.upper()
    if not re.fullmatch(r"[A-Z0-9.\-^=]{1,12}", code):
        raise NotFound
    q = Q.get([code]).get(code)
    cur = "KRW" if Q.is_kr(code) else "USD"
    hold = next((x for x in c.holdings() if x["code"] == code), None)
    name = (hold or {}).get("name") or (q or {}).get("name") or c.name(code)
    pts = c.series(code, 250)
    if not (q or pts):
        raise NotFound
    out = [f'<section class="hero"><small>{h(code)} · {"한국" if cur == "KRW" else "미국"}</small>'
           f'<span class="lbl">{h(name)}</span><div class="quote"><b class="big">{live(code, "price", q, cur)}</b>'
           f'<span class="chg">{live(code, "chg", q, cur)}</span>{market_state(q)}</div></section>']
    lines: dict[str, float | None] = {}
    if hold:
        lines["avg"] = hold["avg_price"]
    rec = c.db.query("SELECT * FROM recommendations WHERE code=? ORDER BY date DESC LIMIT 1", (code,))
    if rec and rec[0]["stop"]:
        lines["stop"] = rec[0]["stop"]
    if pts:
        out.append(card("최근 1년", chart(pts, cur, lines, 220)))
    nws = NW.recent(c.db, code)
    if nws:
        out.append(card("📰 최근 뉴스·공시", '<p class="lead">▲ 주가에 좋은 소식, ▼ 나쁜 소식 (Claude가 제목만 보고 판단). '
                        '아직 성과로 검증된 신호는 아니에요.</p>' + news_list(nws), count=f"{len(nws)}건"))
    if hold:
        out.append(card("내가 가진 만큼", kv([
            ("수량", shares(hold["qty"])), ("평균 산 가격", money(hold["avg_price"], cur)),
            ("평가금액", live(code, "val", q, cur, hold["qty"])), ("손익", live(code, "pnl", q, cur, hold["qty"], hold["avg_price"])),
            ("산 금액", money(hold["avg_price"] * hold["qty"], cur))])))
    if rec:
        r = rec[0]
        out.append(card("시스템 추천", kv([
            ("추천일", h(r["date"])), ("고른 이유", f'<a href="/strategy/{h(r["strategy"])}"><b>{h(LB.strat(r["strategy"]))} ›</b></a>'),
            ("기준가", money(r["entry_ref"], cur)), ("기준가 대비", live(code, "vs", q, cur, ref=r["entry_ref"])),
            ("손절가", money(r["stop"], cur)), ("목표가", money(r["target"], cur) if r["target"] else "없음")])))
    sigs = c.db.query("SELECT strategy, MAX(date) d FROM signals WHERE code=? GROUP BY strategy ORDER BY d DESC LIMIT 8", (code,))
    if sigs:
        out.append(card("신호가 나온 전략", "".join(
            link_row(f"/strategy/{x['strategy']}", avatar(LB.strat(x["strategy"])[:2]), h(LB.strat(x["strategy"])),
                     f"{h(x['d'])} 신호", "") for x in sigs)))
    return name, "".join(out), [code]


def reports(c: Ctx, tab: str) -> tuple[str, str, list[str]]:
    out = [chips([("/reports", "리포트", tab == "list"), ("/reports?t=learn", "학습 일지", tab == "learn"),
                  ("/reports?t=props", "승인 대기", tab == "props"), ("/reports?t=edge", "엣지 맵", tab == "edge"),
                  ("/reports?t=notes", "투자 노트", tab == "notes")])]
    if tab == "learn":
        status_ko = {"open": ("확인 중", ""), "confirmed": ("맞았음", "good"), "rejected": ("틀렸음", "bad"),
                     "expired": ("판정 못 함", "")}
        # merged = the same question asked again in other words; the oldest copy stays and carries the verdict
        ins = c.db.query("SELECT * FROM insights WHERE status != 'merged' ORDER BY id DESC LIMIT 50")
        merged = c.db.query("SELECT COUNT(*) n FROM insights WHERE status = 'merged'")[0]["n"]
        lst = "".join(f'<li class="note"><em class="{status_ko.get(i["status"], ("", ""))[1]}">'
                      f'{status_ko.get(i["status"], (i["status"], ""))[0]}</em> {h(i["text"])}'
                      + (f'<small>→ {h(i["note"])}</small>' if i["note"] else "") + f'<small>{h(kst(i["ts"]))}</small></li>'
                      for i in ins)
        body = f'<ul class="plain">{lst}</ul>' if ins else empty("📒", "시스템이 배운 것이 아직 없어요.")
        if merged:
            body += f'<p class="hint">같은 질문을 말만 바꿔 다시 쓴 {merged}개는 처음 것 하나로 합쳐서 숨겼어요.</p>'
        out.append(card("학습 일지", body))
    elif tab == "props":
        props = c.db.query("SELECT * FROM proposals WHERE status='pending' ORDER BY id")
        lst = "".join(f'<li class="note"><b>#{p["id"]} {h(p["title"])}</b><small>{h((p["rationale"] or "")[:500])}</small>'
                      f'<small>슬랙에서 <code>approve {p["id"]}</code> 또는 <code>reject {p["id"]}</code></small></li>' for p in props)
        out.append(card("승인 대기 제안", f'<ul class="plain">{lst}</ul>' if props else empty("✅", "승인할 제안이 없어요.")))
    elif tab == "notes":
        bt = {}
        for s_ in c.db.strategies(("active", "muted")):
            e = ((s_.get("backtest") or {}).get("full") or {}).get("expectancy_pct")
            if e is not None:
                bt[s_["name"]] = e
        out.append(WS.notes_page(LV.load(c.db), json.loads(c.db.get("playbook") or "null"), bt))
    elif tab == "edge":
        edge = c.db.get("edge_summary")
        out.append(card("엣지 맵", doc_html(DF.parse_text(edge)) if edge else empty("🗺️", "토요일 주간 학습 뒤에 생겨요.")))
    else:
        rs = c.db.query("SELECT * FROM reports ORDER BY id DESC LIMIT 40")
        if rs:
            top = rs[0]
            icon, label = KIND.get(top["kind"], ("📄", top["kind"]))
            data = report_doc(top)
            out.append(card(f"{icon} {label}", f'<p class="lead">{h(kst(top["ts"]))} 작성</p>' + doc_html(data["doc"], data.get("learned"))))
            lst = ""
            for r in rs[1:]:
                icon, label = KIND.get(r["kind"], ("📄", r["kind"]))
                d = report_doc(r)["doc"]
                head = d.get("headline") or next((s["title"] for s in d.get("sections", []) if s.get("title")), "")
                lst += link_row(f"/report/{r['id']}", avatar(icon), h(label), h(head[:40]), "", h(kst(r["ts"], "%-m/%-d %H:%M")))
            if lst:
                out.append(card("지난 리포트", lst))
        else:
            out.append(card("리포트", empty("📰", "아직 리포트가 없어요. 장이 열리는 날 아침·저녁에 생겨요.")))
    return "리포트", "".join(out), []


def sim_method(c: Ctx, mid: str) -> tuple[str, str, list[str]]:
    """/sim/<method>: what one forward mock-investing method holds now and every trade it has closed."""
    L = LV.load(c.db)
    M = (L or {}).get("methods", {}).get(mid)
    if not M:
        raise NotFound
    amt, nm, cur = L["amount"], L.get("names", {}), c.cur
    m = next(x for x in LV.stats(L)["methods"] if x["id"] == mid)
    eq, pick = M["eq"][-1][1], mid == L.get("pick")
    days = [d for d, _ in M["eq"][1:]]
    us_note = " 미국 종목도 원화로 바꿔 넣었다고 치고, 환율 변화는 빼고 계산해요." if c.cfg.is_us else ""
    summary = [("지금 평가액", f'{money(amt * eq, "KRW")} <small class="{tone(eq - 1)}">{pct((eq - 1) * 100, 1)}</small>'),
               (f'{h(L["bench"]["name"])} 대비', f'<span class="{tone(m["vs_bench_pct"])}">{pct(m["vs_bench_pct"], 1)}p</span>'
                if m["vs_bench_pct"] is not None else "-"),
               ("현금", f'{money(amt * M["cash"], "KRW")} <small>{M["cash"] / eq:.0%}</small>' if eq else "-"),
               ("들고 있는 종목", f'{len(M["pos"])}종목 <small>최대 {M["slots"]}종목</small>'),
               ("끝난 거래", f'{m["closed"]}번 · 이긴 비율 {m["win_rate"]:.0f}% · 평균 '
                f'<span class="{tone(m["avg_pct"])}">{pct(m["avg_pct"], 1)}</span>' if m["closed"] else "아직 없어요"),
               ("고점 대비 가장 많이 빠진 폭", f'{m["mdd_pct"]:.1f}%'),
               ("기준", f'{L["last"]} 장 마감 · {_dt_ko(L["start"])}부터 {len(days)}거래일')]
    out = [card(("⭐ " if pick else "") + h(M["label"]),
                f'<p class="lead">{h(M["desc"])}' + (" ⭐는 Claude가 고른 방식이에요." if pick else "") + us_note + '</p>' + kv(summary))]

    codes = [p["code"] for p in M["pos"]]
    qs = Q.get(codes) if codes else {}
    rows = ""
    for i, p in enumerate(sorted(M["pos"], key=lambda p: p["entry_date"], reverse=True)):
        code, q, entry = p["code"], qs.get(p["code"]), p["entry"]
        held = sum(1 for d in days if d >= p["entry_date"])
        items = [("지금 가격", live(code, "price", q, cur) + " " + market_state(q)),
                 ("산 가격 대비", live(code, "vs", q, cur, ref=entry)),
                 ("산 가격", f'{money(entry, cur)} <small>{_md(p["entry_date"])} 시가</small>'),
                 ("손절가", f'{money(p["stop"], cur)} <small class="down">{pct((p["stop"] / entry - 1) * 100, 1)}</small>'),
                 ("목표가", money(p["target"], cur) if p.get("target") else "없음 · 파는 조건이 나올 때까지 보유"),
                 ("넣은 돈", money(amt * p["alloc"], "KRW")),
                 ("장 마감 평가액", f'{money(amt * p["alloc"] * p["last"] / entry, "KRW")} '
                  f'<small class="{tone(p["last"] - entry)}">{pct((p["last"] / entry - 1) * 100, 1)}</small>'),
                 ("들고 있은 날", f'{held}거래일 <small>{_md(p["signal_date"])} 신호</small>')]
        if p.get("news") and p["news"] != "unknown":
            items.append(("살 때 뉴스", NW.LABEL_KO.get(p["news"], p["news"])))
        body = (chart(c.series(code, 120), cur, {"avg": entry, "stop": p["stop"], "target": p.get("target")}) + kv(items)
                + f'<p class="hint"><a href="/strategy/{h(p["strategy"])}"><b>이 전략 보기 ›</b></a> · '
                  f'<a href="/stock/{h(code)}"><b>종목 보기 ›</b></a></p>')
        rows += fold_row(avatar(code), f'{h(code)} <small class="nm">{h(nm.get(code) or c.name(code))}</small>',
                         h(LB.strat(p["strategy"])),
                         live(code, "price", q, cur), "산 가격 대비 " + live(code, "vs", q, cur, ref=entry), body, i == 0)
    out.append(card("지금 들고 있는 것", rows or empty("💵", "지금은 현금만 들고 있어요."),
                    count=f'{len(M["pos"])}종목' if M["pos"] else ""))

    buys = {(e[2], e[3], e[0]): e[4] for e in M["events"] if e[1] == "b"}
    trades = ""
    for e in reversed([e for e in M["events"] if e[1] == "s"]):
        code, why = e[2], WS.WHY_KO.get(e[6], e[6])
        bought = e[7] if len(e) > 7 else None
        buy = buys.get((code, e[3], bought))
        items = [("산 가격", f'{money(buy, cur)} <small>{_md(bought)} 시가</small>' if buy else "-"),
                 ("판 가격", f'{money(e[4], cur)} <small>{_md(e[0])} · {why}</small>'),
                 ("수익률", f'<span class="{tone(e[5])}">{pct(e[5], 2)}</span> <small>비용 빼기 전 '
                  f'{pct((e[4] / buy - 1) * 100, 2) if buy else "-"}</small>')]
        if bought:
            items.append(("들고 있은 날", f'{sum(1 for d in days if bought <= d <= e[0])}거래일'))
        if len(e) > 8 and e[8]:
            items.append(("살 때 시장", LB.regime(e[8])))
        if len(e) > 9 and e[9] != "unknown":
            items.append(("살 때 뉴스", NW.LABEL_KO.get(e[9], e[9])))
        body = (kv(items) + f'<p class="hint"><a href="/strategy/{h(e[3])}"><b>이 전략 보기 ›</b></a> · '
                f'<a href="/stock/{h(code)}"><b>종목 보기 ›</b></a></p>')
        trades += fold_row(avatar(code), f'{h(code)} <small class="nm">{h(nm.get(code) or c.name(code))}</small>',
                           f'{h(LB.strat(e[3]))} · {why}', f'<span class="{tone(e[5])}">{pct(e[5], 1)}</span>',
                           (f"{_md(bought)} → " if bought else "") + _md(e[0]), body)
    n_closed = sum(1 for e in M["events"] if e[1] == "s")
    out.append(card("끝난 거래", ('<p class="lead">수익률은 수수료·슬리피지를 뺀 값이에요.</p>' + trades) if trades
                    else empty("⏳", "아직 판 종목이 없어요."), count=f"{n_closed}번" if n_closed else ""))
    return M["label"], "".join(out), codes


def _dt_ko(s: str) -> str:
    return f"{int(s[5:7])}월 {int(s[8:10])}일"


def _md(s: str) -> str:
    return s[5:10].replace("-", "/")


def report(c: Ctx, rid: str) -> tuple[str, str, list[str]]:
    if not rid.isdigit():
        raise NotFound
    r = c.db.query("SELECT * FROM reports WHERE id=?", (int(rid),))
    if not r:
        raise NotFound
    icon, label = KIND.get(r[0]["kind"], ("📄", r[0]["kind"]))
    data = report_doc(r[0])
    return label, card(f"{icon} {label}", f'<p class="lead">{h(kst(r[0]["ts"]))} 작성</p>' + doc_html(data["doc"], data.get("learned"))), []


def route(cfg: Config, db: DB, path: str, query: dict, kr: tuple | None = None) -> str:
    """``kr`` = (Config, DB) of the Korean-market instance; its pages live under /kr/..."""
    parts = [unquote(p) for p in path.strip("/").split("/") if p]
    base, markets = "", [(cfg, db)] + ([kr] if kr else [])
    if parts[:1] == ["kr"]:
        parts, base = parts[1:], "/kr"
        if not kr:
            body = card("🇰🇷 국내 스윙 모드", empty("⏳", "국내 시장 데이터를 준비하고 있어요. 첫 수집은 30분~1시간쯤 걸려요."))
            return page("국내", body, "/", None, [], base, "/")
        c = Ctx(kr[0], kr[1], hdb=db, markets=markets)
    else:
        c = Ctx(cfg, db, markets=markets)
    tab, back = "/", None
    if not parts:
        title, body, codes = home(c)
    elif parts == ["assets"]:
        c = Ctx(cfg, db, markets=markets)
        (title, body, codes), tab = assets(c), "/assets"
    elif parts == ["picks"]:
        (title, body, codes), tab = picks(c), "/picks"
    elif parts == ["strategies"]:
        (title, body, codes), tab = strategies(c), "/strategies"
    elif len(parts) == 2 and parts[0] == "strategy":
        (title, body, codes), tab, back = strategy(c, parts[1]), "/strategies", "/strategies"
    elif len(parts) == 2 and parts[0] == "stock":
        if base and not Q.is_kr(parts[1]):  # US ticker reached from a KR page (e.g. 내 자산)
            c = Ctx(cfg, db, markets=markets)
        (title, body, codes), tab, back = stock(c, parts[1]), "", "javascript:history.back()"
    elif parts == ["news"]:
        mk = NW.recent(c.db, "", 60)
        title, codes, tab = "시장 뉴스" + (" · 국내" if base else ""), [], "/"
        body = card("📰 시장 뉴스", news_list(mk, 60) or empty("📰", "저녁 배치가 한 번 돌면 뉴스가 쌓여요."), count=f"{len(mk)}건")
    elif parts == ["sim"]:
        title, body, codes, tab = ("타임머신 모의투자" + (" · 국내" if base else ""), WS.live_page(LV.load(c.db)) + WS.page_body(WS.load(c.db)), [], "/sim")
    elif len(parts) == 2 and parts[0] == "sim":
        (title, body, codes), tab, back = sim_method(c, parts[1]), "/sim", "/sim"
    elif parts == ["reports"]:
        (title, body, codes), tab = reports(c, (query.get("t") or ["list"])[0]), "/reports"
    elif len(parts) == 2 and parts[0] == "report":
        (title, body, codes), tab, back = report(c, parts[1]), "/reports", "/reports"
    else:
        raise NotFound
    if base:  # keep in-page links inside /kr (내 자산 and the API are shared)
        body = re.sub(r'href="/(?!kr/|api/)', 'href="/kr/', body)
        if back and back.startswith("/"):
            back = "/kr" + back
    switch = None
    if kr and tab in ("/", "/assets", "/picks", "/strategies", "/sim", "/reports"):
        rest = "/" + "/".join(parts) if parts else "/"
        if parts[:1] in (["strategy"], ["report"], ["stock"]):
            rest = tab
        switch = rest if base else ("/kr" + rest).rstrip("/") or "/kr"
    return page(title, body, tab, back, codes, base, switch)


# ---------------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    cfg: Config
    kr_cfg: Config | None = None
    user = "me"
    password: str | None = None

    def log_message(self, fmt, *args):
        pass

    def _authorized(self) -> bool:
        if not self.password:
            return True
        got = self.headers.get("Authorization", "")
        want = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        return hmac.compare_digest(got.encode(), want.encode())

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="stockmining", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        query = parse_qs(url.query)
        if url.path == "/api/sim":
            kr = (query.get("m") or [""])[0] == "kr" and self.kr_cfg is not None and self.kr_cfg.path("db_path").exists()
            db = ReadOnlyDB((self.kr_cfg if kr else self.cfg).path("db_path"))
            try:
                return self._send(200, db.get("replay") or "null", "application/json")
            finally:
                db.conn.close()
        if url.path == "/api/quotes":
            codes = [x for x in (query.get("c") or [""])[0].split(",") if re.fullmatch(r"[A-Za-z0-9.\-^=]{1,12}", x)][:60]
            return self._send(200, json.dumps(Q.get(codes)), "application/json")
        try:
            db = ReadOnlyDB(self.cfg.path("db_path"))
            kdb = None
            try:
                if self.kr_cfg is not None and self.kr_cfg.path("db_path").exists():
                    try:
                        kdb = ReadOnlyDB(self.kr_cfg.path("db_path"))
                        kdb.query("SELECT 1 FROM strategies LIMIT 1")
                    except sqlite3.Error:
                        kdb = None
                html = route(self.cfg, db, url.path, query, (self.kr_cfg, kdb) if kdb else None)
            finally:
                db.conn.close()
                if kdb:
                    kdb.conn.close()
            self._send(200, html, "text/html; charset=utf-8")
        except NotFound:
            self._send(404, page("없는 페이지", card("없는 페이지", empty("🤔", "찾는 페이지가 없어요.")), "", "/"),
                       "text/html; charset=utf-8")
        except Exception:
            log.exception("render failed: %s", self.path)
            self._send(500, "페이지를 그리지 못했어요. 서버 로그를 확인하세요.", "text/plain; charset=utf-8")

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
    kp = os.environ.get("STOCKMINING_CONFIG_KR")
    if kp and Path(kp).exists():
        from .config import load_config
        Handler.kr_cfg = load_config(kp)
        log.info("korean market pages enabled (%s)", kp)
    log.info("dashboard on http://%s:%s", host, port)
    ThreadingHTTPServer((host, port), Handler).serve_forever()

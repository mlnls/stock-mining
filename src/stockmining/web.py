"""Read-only web dashboard: ``stockmining web`` (stdlib HTTP server, server-rendered HTML).

Shows the same things the Slack commands do (recs, positions, strategies, paper trades, reports, learning
log, proposals). It never writes: the DB is opened read-only. Access needs HTTP basic auth with
WEB_PASSWORD (user WEB_USER, default "me"); without a password the server refuses to start unless it only
listens on localhost.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import sqlite3
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import journal as J
from .config import Config
from .db import DB

log = logging.getLogger(__name__)


class ReadOnlyDB(DB):
    def __init__(self, path: str | Path):  # skip schema/WAL setup: never write from the web process
        self.path = Path(path)
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
        self.conn.row_factory = sqlite3.Row


def h(v) -> str:
    return escape("" if v is None else str(v))


def money(v, us: bool) -> str:
    if v is None:
        return "-"
    return f"${v:,.2f}" if us else f"{v:,.0f}원"


def pct(v, digits: int = 1, sign: bool = True) -> str:
    if v is None:
        return "-"
    return f"{v:+.{digits}f}%" if sign else f"{v:.{digits}f}%"


def pnl_cls(v) -> str:
    return "" if v is None else ("up" if v > 0 else "down" if v < 0 else "")


def num(v, digits: int = 2) -> str:
    return "-" if v is None else f"{v:.{digits}f}"


def sparkline(closes: list[float], stop: float | None = None, w: int = 132, hgt: int = 34) -> str:
    """Single-series 60-day close line; dashed line at the stop if it falls in range. Hover shows range."""
    if len(closes) < 2:
        return ""
    lo, hi = min(closes + ([stop] if stop else [])), max(closes)
    span = (hi - lo) or 1
    y = lambda v: round(hgt - 3 - (v - lo) / span * (hgt - 6), 1)  # noqa: E731
    pts = " ".join(f"{round(i / (len(closes) - 1) * (w - 4) + 2, 1)},{y(c)}" for i, c in enumerate(closes))
    stop_line = (f'<line x1="2" x2="{w - 2}" y1="{y(stop)}" y2="{y(stop)}" class="spark-stop"/>'
                 if stop and lo <= stop <= hi else "")
    lx, ly = pts.split()[-1].split(",")
    tip = f"최근 {len(closes)}일 종가: 최저 {min(closes):,.2f} · 최고 {hi:,.2f} · 마지막 {closes[-1]:,.2f}"
    if stop:
        tip += f" · 점선 = 손절 {stop:,.2f}"
    return (f'<svg class="spark" width="{w}" height="{hgt}" viewBox="0 0 {w} {hgt}" role="img" '
            f'aria-label="{h(tip)}"><title>{h(tip)}</title>{stop_line}'
            f'<polyline points="{pts}" class="spark-line"/><circle cx="{lx}" cy="{ly}" r="2.5" class="spark-dot"/></svg>')


def closes_for(db: DB, codes: list[str], n: int = 60) -> dict[str, list[float]]:
    out = {}
    for c in codes:
        rows = db.query("SELECT close FROM prices WHERE code=? ORDER BY date DESC LIMIT ?", (c, n))
        out[c] = [r["close"] for r in reversed(rows) if r["close"] is not None]
    return out


def table(head: list[str], rows: list[list[str]], empty: str, num_cols: set[int] = frozenset()) -> str:
    if not rows:
        return f'<p class="empty">{h(empty)}</p>'
    th = "".join(f'<th class="{"n" if i in num_cols else ""}">{h(x)}</th>' for i, x in enumerate(head))
    body = "".join("<tr>" + "".join(f'<td class="{"n" if i in num_cols else ""}">{c}</td>' for i, c in enumerate(r))
                   + "</tr>" for r in rows)
    return f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


REGIME = {"bull": "상승장", "neutral": "중립", "bear": "하락장"}
STATUS = {"active": "사용 중", "muted": "음소거", "retired": "퇴역"}


def render(cfg: Config, db: DB) -> str:
    us = cfg.is_us
    info = db.ticker_info()
    name = lambda c: info.get(c, {}).get("name") or c  # noqa: E731
    ctx = json.loads(db.get("last_evening_ctx") or "{}")
    last_day = db.last_price_date()

    # ---- header facts
    idx = db.load_index(cfg["regime_index"])
    from . import regime as RG
    market = RG.describe(idx) if not idx.empty else {}
    acct = cfg["account"]
    facts = [
        ("데이터 기준일", h(last_day or "-")),
        ("시장 국면", f'{h(REGIME.get(market.get("regime"), market.get("regime") or "-"))}'
                    f'<small>{h(cfg["regime_index"])} {num(market.get("close"))} · 20일 {pct(market.get("chg_20d_pct"))}</small>'),
        ("운용 자금", money(acct.get("capital"), us)),
        ("투자 성향", h({"aggressive": "공격형", "normal": "기본형"}.get(cfg["profile"], cfg["profile"]))),
    ]
    facts_html = "".join(f'<div class="fact"><span>{k}</span><b>{v}</b></div>' for k, v in facts)

    # ---- recommendations
    rdate = db.query("SELECT MAX(date) d FROM recommendations")[0]["d"]
    recs = db.query("SELECT * FROM recommendations WHERE date=? ORDER BY combined_score DESC", (rdate,)) if rdate else []
    closes = closes_for(db, [r["code"] for r in recs])
    rec_rows = []
    for r in recs:
        risk = (r["entry_ref"] - r["stop"]) * r["shares"] if r["entry_ref"] and r["stop"] and r["shares"] else None
        rec_rows.append([
            f'<b>{h(r["code"])}</b><small>{h(name(r["code"]))}</small>', h(r["strategy"]),
            sparkline(closes.get(r["code"], []), r["stop"]),
            money(r["entry_ref"], us), money(r["stop"], us), money(r["target"], us), h(r["shares"]),
            money(risk, us),
            f'<span class="tag warn">⛔ 보류</span> {h(r["flagged"])}' if r["flagged"] else '<span class="tag">후보</span>'])
    recs_html = table(["종목", "전략", "최근 60일", "기준가", "손절", "목표", "수량", "최대 손실", "상태"], rec_rows,
                      "아직 추천이 없어요. 첫 저녁 배치 후 채워집니다.", {3, 4, 5, 6, 7})

    # ---- positions & round trips
    pos, trips = J.positions_and_roundtrips(db)
    if pos:
        J.position_alerts(pos, db.load_panel(list(pos), None))
    pcl = closes_for(db, list(pos))
    pos_rows = [[
        f'<b>{h(c)}</b><small>{h(name(c))}</small>', sparkline(pcl.get(c, []), p["plan_stop"]), h(p["qty"]),
        money(p["avg_price"], us), money(p.get("last_close"), us),
        f'<span class="{pnl_cls(p.get("pnl_pct"))}">{pct(p.get("pnl_pct"), 2)}</span>',
        money(p["plan_stop"], us) if p["plan_stop"] else '<span class="tag warn">미설정</span>',
        money(p["plan_target"], us), h(p["strategy"] or "-"), h(p["first_buy"])] for c, p in pos.items()]
    pos_html = table(["종목", "최근 60일", "수량", "평단", "종가", "손익", "손절", "목표", "전략", "첫 매수"], pos_rows,
                     "보유 종목이 없어요. 슬랙에서 buy 티커 가격 수량 으로 기록하면 여기 나타나요.", {2, 3, 4, 5, 6, 7})
    trip_rows = [[h(t["sell_date"]), f'<b>{h(t["code"])}</b>', h(t["qty"]), money(t["buy"], us), money(t["sell"], us),
                  f'<span class="{pnl_cls(t["ret_pct"])}">{pct(t["ret_pct"], 2)}</span>', h(t["strategy"] or "-"),
                  h(t["sell_note"] or "")] for t in reversed(trips[-20:])]
    trips_html = table(["매도일", "종목", "수량", "매수가", "매도가", "수익률", "전략", "메모"], trip_rows,
                       "완료된 매매가 아직 없어요.", {2, 3, 4, 5})

    # ---- strategies
    weights = ctx.get("weights", {})
    strat_rows = []
    for s in db.strategies(("active", "muted", "retired")):
        bt = s["backtest"] or {}
        f, o = bt.get("full", {}), bt.get("oos", {})
        p = db.query("SELECT COUNT(*) n, AVG(ret_pct) a, AVG(ret_pct>0)*100 w FROM paper_trades "
                     "WHERE strategy=? AND status='closed'", (s["name"],))[0]
        po = db.query("SELECT COUNT(*) n FROM paper_trades WHERE strategy=? AND status IN ('open','pending')",
                      (s["name"],))[0]["n"]
        pf = f.get("profit_factor")
        strat_rows.append([
            f'<b>{h(s["name"])}</b><small>{h((s["spec"].get("description") or "").strip()[:90])}</small>',
            f'<span class="tag {"" if s["status"] == "active" else "mute"}">{h(STATUS.get(s["status"], s["status"]))}</span>',
            pct(weights[s["name"]] * 100, 0, False) if s["name"] in weights else "-",
            h(f.get("trades", "-")), pct(f.get("win_rate"), 0, False),
            f'<span class="{pnl_cls(f.get("expectancy_pct"))}">{pct(f.get("expectancy_pct"), 2)}</span>',
            f'<span class="{"up" if pf and pf > 1 else "down" if pf else ""}">{num(pf)}</span>',
            num(o.get("profit_factor")), pct(f.get("cagr_pct"), 0), pct(f.get("mdd_pct"), 0, False),
            f'{p["n"]}건 · <span class="{pnl_cls(p["a"])}">{pct(p["a"], 2)}</span>' + (f'<small>진행 {po}</small>' if po else ""),
        ])
    strat_html = table(["전략", "상태", "비중", "거래", "승률", "거래당", "PF", "최근 PF", "연수익", "최대 낙폭", "페이퍼"],
                       strat_rows, "전략이 없어요.", {2, 3, 4, 5, 6, 7, 8, 9})

    # ---- paper trades
    paper = db.query("SELECT * FROM paper_trades WHERE status IN ('open','pending') ORDER BY signal_date DESC LIMIT 30")
    paper_closed = db.query("SELECT * FROM paper_trades WHERE status='closed' ORDER BY exit_date DESC LIMIT 30")
    paper_rows = [[h(p["signal_date"]), f'<b>{h(p["code"])}</b>', h(p["strategy"]),
                   h("대기" if p["status"] == "pending" else "보유"), money(p["entry_price"], us), money(p["stop"], us),
                   money(p["target"], us)] for p in paper]
    paper_html = table(["신호일", "종목", "전략", "상태", "진입가", "손절", "목표"], paper_rows,
                       "진행 중인 가상 매매가 없어요.", {4, 5, 6})
    pc_rows = [[h(p["exit_date"]), f'<b>{h(p["code"])}</b>', h(p["strategy"]), money(p["entry_price"], us),
                money(p["exit_price"], us), f'<span class="{pnl_cls(p["ret_pct"])}">{pct(p["ret_pct"], 2)}</span>',
                h(p["exit_reason"])] for p in paper_closed]
    pc_html = table(["청산일", "종목", "전략", "진입가", "청산가", "수익률", "사유"], pc_rows, "아직 청산된 가상 매매가 없어요.",
                    {3, 4, 5})

    # ---- reports
    kinds = {"evening": "저녁 리포트", "morning": "아침 브리핑", "weekly": "주간 학습", "ask": "질문"}
    rep_html = ""
    for k, label in kinds.items():
        r = db.query("SELECT * FROM reports WHERE kind=? ORDER BY id DESC LIMIT 1", (k,))
        if r:
            rep_html += (f'<details {"open" if not rep_html else ""}><summary>{label}<small>{h(r[0]["ts"][:16].replace("T", " "))}'
                         f'</small></summary><div class="report">{h(r[0]["body"])}</div></details>')
    rep_html = rep_html or '<p class="empty">아직 리포트가 없어요. 오늘 밤 아침 브리핑, 내일 아침 저녁 리포트가 생겨요.</p>'

    # ---- insights / proposals / edge
    icon = {"open": "⏳ 검증 중", "confirmed": "✅ 확인", "rejected": "❌ 기각"}
    kind_ko = {"observation": "관찰", "hypothesis": "가설", "lesson": "교훈"}
    ins = db.query("SELECT * FROM insights ORDER BY id DESC LIMIT 30")
    ins_html = "".join(
        f'<li><span class="tag {"" if i["status"] == "confirmed" else "mute" if i["status"] == "rejected" else ""}">'
        f'{icon.get(i["status"], i["status"])}</span> <b>{h(kind_ko.get(i["kind"], i["kind"]))}</b> {h(i["text"])}'
        + (f'<small>→ {h(i["note"])}</small>' if i["note"] else "") + f'<small>{h(i["ts"][:10])}</small></li>'
        for i in ins) or '<p class="empty">학습 일지가 아직 비어 있어요.</p>'
    if ins:
        ins_html = f'<ul class="list">{ins_html}</ul>'
    props = db.query("SELECT * FROM proposals WHERE status='pending' ORDER BY id")
    prop_html = "".join(
        f'<li><b>#{p["id"]} [{h(p["kind"])}] {h(p["title"])}</b><small>{h((p["rationale"] or "")[:400])}</small>'
        f'<small>슬랙에서 <code>approve {p["id"]}</code> 또는 <code>reject {p["id"]}</code></small></li>' for p in props)
    prop_html = f'<ul class="list">{prop_html}</ul>' if props else '<p class="empty">승인 대기 중인 제안이 없어요.</p>'
    edge = db.get("edge_summary")
    edge_html = f'<div class="report">{h(edge)}</div>' if edge else '<p class="empty">엣지 맵은 토요일 주간 학습 후에 생겨요.</p>'

    alerts = ctx.get("alerts") or []
    alert_html = "".join(f'<div class="alert">{h(a)}</div>' for a in alerts)

    sections = [
        ("recs", f"추천 종목 <small>{h(rdate or '')} 장 마감 기준 · 다음 거래일 매수 후보</small>", recs_html),
        ("pos", "보유 포지션", pos_html + "<h3>최근 완료 매매</h3>" + trips_html),
        ("reports", "리포트", rep_html),
        ("strats", "전략 현황 <small>백테스트(전 기간) · 최근 PF = 표본 외 구간 · 페이퍼 = 실제 가상매매</small>", strat_html),
        ("paper", "페이퍼 트레이딩", "<h3>진행 중</h3>" + paper_html + "<h3>최근 청산</h3>" + pc_html),
        ("learn", "학습 일지", ins_html),
        ("props", "승인 대기 제안", prop_html),
        ("edge", "엣지 맵", edge_html),
    ]
    nav = "".join(f'<a href="#{i}">{t.split(" <")[0]}</a>' for i, t, _ in sections)
    body = "".join(f'<section id="{i}"><h2>{t}</h2>{c}</section>' for i, t, c in sections)
    return PAGE.format(facts=facts_html, nav=nav, body=body, alerts=alert_html)


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Stock Mining</title>
<style>
:root{{color-scheme:light;--bg:#f6f6f4;--card:#fcfcfb;--line:#e4e3df;--ink:#0b0b0b;--ink2:#52514e;--ink3:#8a8984;
--accent:#2a78d6;--up:#0f7b3f;--down:#c4312f;--warn:#9a5b00;--warnbg:#fdf1dc;--tag:#eceae5}}
@media (prefers-color-scheme:dark){{:root{{color-scheme:dark;--bg:#121211;--card:#1a1a19;--line:#2e2e2c;--ink:#f0efec;
--ink2:#c3c2b7;--ink3:#8d8c85;--accent:#3987e5;--up:#4cc38a;--down:#f07170;--warn:#f0b450;--warnbg:#3a2c12;--tag:#2a2a28}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,
"Apple SD Gothic Neo","Pretendard","Noto Sans KR",sans-serif;-webkit-font-smoothing:antialiased}}
header{{max-width:1180px;margin:0 auto;padding:20px 16px 8px}}h1{{font-size:20px;margin:0 0 12px;letter-spacing:-.01em}}
.facts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}}
.fact{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}}
.fact span{{display:block;font-size:12px;color:var(--ink2)}}.fact b{{font-size:17px;font-variant-numeric:tabular-nums}}
.fact small{{display:block;font-size:12px;color:var(--ink3);font-weight:400}}
nav{{position:sticky;top:0;z-index:2;background:color-mix(in srgb,var(--bg) 92%,transparent);backdrop-filter:blur(6px);
border-bottom:1px solid var(--line)}}nav div{{max-width:1180px;margin:0 auto;padding:8px 16px;display:flex;gap:4px;overflow-x:auto}}
nav a{{white-space:nowrap;color:var(--ink2);text-decoration:none;padding:4px 10px;border-radius:999px;font-size:13px}}
nav a:hover{{background:var(--tag);color:var(--ink)}}
main{{max-width:1180px;margin:0 auto;padding:8px 16px 48px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:14px 0;scroll-margin-top:52px}}
h2{{font-size:16px;margin:0 0 10px}}h2 small,summary small{{font-weight:400;color:var(--ink3);font-size:12px;margin-left:8px}}
h3{{font-size:13px;color:var(--ink2);margin:16px 0 6px}}
.tw{{overflow-x:auto;margin:0 -4px}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
th{{text-align:left;font-size:12px;font-weight:500;color:var(--ink2);padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}
td{{padding:8px;border-bottom:1px solid var(--line);vertical-align:middle;white-space:nowrap}}tr:last-child td{{border-bottom:0}}
tbody tr:hover{{background:color-mix(in srgb,var(--tag) 55%,transparent)}}
td small{{display:block;color:var(--ink3);font-size:12px;white-space:normal;max-width:320px}}.n{{text-align:right}}
.up{{color:var(--up)}}.down{{color:var(--down)}}
.tag{{display:inline-block;font-size:12px;padding:1px 8px;border-radius:999px;background:var(--tag);color:var(--ink2)}}
.tag.warn{{background:var(--warnbg);color:var(--warn)}}.tag.mute{{opacity:.7}}
.empty{{color:var(--ink3);margin:6px 0}}.alert{{background:var(--warnbg);color:var(--warn);border-radius:10px;padding:10px 12px;margin:10px 0 0}}
.spark{{display:block}}.spark-line{{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}}
.spark-dot{{fill:var(--accent);stroke:var(--card);stroke-width:2}}.spark-stop{{stroke:var(--ink3);stroke-width:1;stroke-dasharray:3 3}}
details{{border-top:1px solid var(--line)}}details:first-of-type{{border-top:0}}summary{{cursor:pointer;padding:10px 0;font-weight:600}}
.report{{white-space:pre-wrap;word-break:break-word;color:var(--ink);background:var(--bg);border-radius:8px;padding:12px;margin-bottom:12px;
font-size:13.5px;line-height:1.65}}
.list{{list-style:none;margin:0;padding:0}}.list li{{padding:9px 0;border-bottom:1px solid var(--line)}}.list li:last-child{{border:0}}
.list small{{display:block;color:var(--ink3);font-size:12px;margin-top:2px}}code{{background:var(--tag);padding:1px 5px;border-radius:4px}}
footer{{text-align:center;color:var(--ink3);font-size:12px;padding:0 16px 32px}}
</style></head><body>
<header><h1>Stock Mining</h1><div class="facts">{facts}</div>{alerts}</header>
<nav><div>{nav}</div></nav>
<main>{body}</main>
<footer>보기 전용 · 매매 기록과 승인은 슬랙에서 · 투자 판단과 주문은 직접 하세요</footer>
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

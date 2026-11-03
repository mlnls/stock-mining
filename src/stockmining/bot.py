"""Telegram bot (long polling — no inbound port needed). Only the configured chat is served."""
from __future__ import annotations

import json
import logging
import re
import time
import traceback

from . import analyst as AI
from . import journal as J
from .jobs import App

log = logging.getLogger(__name__)

HELP = """명령어 (코드 = 티커, 예: NVDA)
/buy 코드 가격 수량 [메모]   매수 기록 (최근 추천이면 손절·목표 자동 연결)
/sell 코드 가격 수량 [메모]  매도 기록
/stop 코드 가격             손절가 수정
/note 코드 메모             메모
/pos                        보유 포지션
/recs                       최근 추천
/proposals                  대기 중인 제안
/approve 번호 · /reject 번호
/strats                     전략 현황
/insights                   학습 일지 (관찰·가설·교훈과 검증 결과)
/edge                       엣지 맵 (전략별로 돈이 벌리고 잃는 조건)
/report                     최근 리포트 다시 보기
/ask 질문                   Claude에게 질문 (웹 검색 포함)"""


def _code(s: str, app: App) -> str | None:
    s = s.strip()
    if app.cfg.is_us and re.fullmatch(r"[A-Za-z]{1,5}([.-][A-Za-z])?", s):
        return s.upper().replace(".", "-")
    if re.fullmatch(r"\d{6}", s):
        return s
    rows = app.db.query("SELECT code FROM tickers WHERE name=?", (s,))
    return rows[0]["code"] if rows else None


def handle(app: App, text: str) -> str:
    parts = text.strip().split()
    if not parts:
        return HELP
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    try:
        if cmd in ("/start", "/help"):
            return HELP
        if cmd in ("/buy", "/sell"):
            if len(args) < 3:
                return f"형식: {cmd} 코드 가격 수량 [메모]"
            code = _code(args[0], app)
            if not code:
                return f"종목을 찾을 수 없음: {args[0]}"
            price = float(args[1].replace(",", "").lstrip("$"))
            q = float(args[2].replace(",", ""))
            qty = int(q) if q.is_integer() else q  # 소수점 수량 허용 (예: 0.35주)
            res = J.record(app.db, cmd[1:], code, price, qty, " ".join(args[3:]))
            msg = f"기록: {cmd[1:]} {code} {app.fmt(price)} × {qty}"
            if res["strategy"]:
                msg += f"\n전략 {res['strategy']} · 손절 {app.fmt(res['plan_stop'])}"
                if res["plan_target"]:
                    msg += f" · 목표 {app.fmt(res['plan_target'])}"
            elif cmd == "/buy":
                msg += "\n⚠️ 최근 시스템 추천이 아닌 매수입니다. /stop 코드 가격 으로 손절가를 꼭 정하세요."
            return msg
        if cmd == "/stop":
            code = _code(args[0], app) if args else None
            if not code or len(args) < 2:
                return "형식: /stop 코드 가격"
            return "손절가 수정 완료" if J.set_stop(app.db, code, float(args[1].replace(",", ""))) else "매수 기록 없음"
        if cmd == "/note":
            code = _code(args[0], app) if args else None
            J.record(app.db, "note", code or "-", None, None, " ".join(args[1:] if code else args))
            return "메모 저장"
        if cmd == "/pos":
            pos, _ = J.positions_and_roundtrips(app.db)
            if not pos:
                return "보유 없음"
            panel = app.db.load_panel(list(pos), None)
            J.position_alerts(pos, panel, app.fmt)
            info = app.db.ticker_info()
            return "\n".join(
                f"{info.get(c, {}).get('name', c)}({c}) {p['qty']}주 평단 {app.fmt(p['avg_price'])} "
                f"종가 {app.fmt(p.get('last_close'))} ({p.get('pnl_pct')}%) "
                f"손절 {app.fmt(p['plan_stop']) if p['plan_stop'] else '미설정'}"
                for c, p in pos.items())
        if cmd == "/recs":
            last = app.db.query("SELECT MAX(date) d FROM recommendations")[0]["d"]
            rows = app.db.query("SELECT * FROM recommendations WHERE date=?", (last,))
            info = app.db.ticker_info()
            return f"{last} 추천\n" + ("\n".join(
                f"{info.get(r['code'], {}).get('name', r['code'])}({r['code']}) [{r['strategy']}] 기준 {app.fmt(r['entry_ref'])} "
                f"손절 {app.fmt(r['stop'])} 수량 {r['shares']}" + (f" ⛔{r['flagged']}" if r["flagged"] else "")
                for r in rows) or "없음")
        if cmd == "/proposals":
            rows = app.db.query("SELECT * FROM proposals WHERE status='pending' ORDER BY id")
            return "\n\n".join(f"#{r['id']} [{r['kind']}] {r['title']}\n{(r['rationale'] or '')[:300]}" for r in rows) \
                or "대기 중인 제안 없음"
        if cmd in ("/approve", "/reject"):
            if not args or not args[0].lstrip("#").isdigit():
                return f"형식: {cmd} 번호"
            return app.decide(int(args[0].lstrip("#")), cmd == "/approve")
        if cmd == "/strats":
            rows = app.db.strategies(("active", "muted", "retired"))
            out = []
            for s in rows:
                f = (s["backtest"] or {}).get("full", {})
                n = app.db.query("SELECT COUNT(*) n, AVG(ret_pct) a FROM paper_trades WHERE strategy=? AND status='closed'",
                                 (s["name"],))[0]
                out.append(f"{s['name']} [{s['status']}/{s['source']}] BT PF {f.get('profit_factor')} "
                           f"기대값 {f.get('expectancy_pct')}% | 페이퍼 {n['n']}건 {round(n['a'] or 0, 2)}%")
            return "\n".join(out)
        if cmd == "/insights":
            rows = app.db.query("SELECT * FROM insights ORDER BY id DESC LIMIT 15")
            icon = {"open": "⏳", "confirmed": "✅", "rejected": "❌"}
            return "\n".join(f"{icon[r['status']]} #{r['id']} [{r['kind']}] {r['text']}"
                             + (f"\n   → {r['note']}" if r["note"] else "") for r in rows) or "학습 일지 비어 있음"
        if cmd == "/edge":
            return app.db.get("edge_summary") or "엣지 맵 없음 (주간 배치 후 생성)"
        if cmd == "/report":
            r = app.db.query("SELECT * FROM reports WHERE kind IN ('evening','morning','weekly') ORDER BY id DESC LIMIT 1")
            return r[0]["body"] if r else "리포트 없음"
        if cmd == "/ask":
            if not app.claude_ready:
                return "Claude 미설정 (Claude Code + CLAUDE_CODE_OAUTH_TOKEN 또는 ANTHROPIC_API_KEY 필요)"
            ctx = json.loads(app.db.get("last_evening_ctx") or "{}")
            ans = AI.ask(app.cfg.raw, app.principles(), ctx, " ".join(args))
            app.db.add_report("ask", f"Q: {' '.join(args)}\n\n{ans}", app.now().isoformat(timespec="seconds"))
            return ans
        return HELP
    except Exception as e:
        log.error("command failed: %s\n%s", text, traceback.format_exc())
        return f"오류: {e}"


def run(app: App) -> None:
    if not app.tg.enabled:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 필요합니다")
    offset = int(app.db.get("tg_offset", "0") or 0) or None
    log.info("bot started")
    while True:
        try:
            updates = app.tg.updates(offset)
        except Exception as e:
            log.warning("poll failed: %s", e)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            app.db.set("tg_offset", str(offset))
            msg = u.get("message") or u.get("edited_message") or {}
            chat = str(msg.get("chat", {}).get("id", ""))
            if chat != str(app.cfg.telegram_chat_id):
                log.warning("ignored message from chat %s", chat)
                continue
            text = msg.get("text") or ""
            if text:
                app.tg.send(handle(app, text))

"""Chat bot for Slack (Socket Mode) or Telegram (long polling). Both connect out from the server, so no
inbound port is needed. Only the configured user (Slack) / chat (Telegram) is served."""
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

HELP = """명령어 (코드 = 티커, 예: NVDA). Slack에서는 봇 DM에 '/' 없이(buy NVDA 180 5) 또는 /sm buy ... 로

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
/mock                       지금부터 모의투자 순위 (방식별 가상 1000만원)
/playbook                   투자 노트 (모의투자로 배운 것 + 직접 투자 준비도)
/report                     최근 리포트 다시 보기
/ask 질문                   Claude에게 질문 (웹 검색 포함)
/hold                       내 자산(장기 보유) 목록
/hold 코드 수량 평단         내 자산 등록·수정 (예: hold 379800 19 23965)
/hold rm 코드               내 자산 삭제

국내 시장(스윙 모드)은 앞에 kr 을 붙여요: kr recs · kr pos · kr buy 005930 71000 3 · kr strats"""

_KR = None


def kr_app() -> App | None:
    """Second instance for the Korean market, if configured (STOCKMINING_CONFIG_KR)."""
    global _KR
    import os
    from pathlib import Path
    p = os.environ.get("STOCKMINING_CONFIG_KR")
    if _KR is None and p and Path(p).exists():
        from .config import load_config
        _KR = App(load_config(p))
    return _KR


def _code(s: str, app: App) -> str | None:
    s = s.strip()
    if app.cfg.is_us and re.fullmatch(r"[A-Za-z]{1,5}([.-][A-Za-z])?", s):
        return s.upper().replace(".", "-")
    if re.fullmatch(r"\d{6}", s):
        return s
    rows = app.db.query("SELECT code FROM tickers WHERE name=?", (s,))
    return rows[0]["code"] if rows else None


def handle(app: App, text: str) -> str:
    text = text.strip()
    if text and not text.startswith("/"):
        text = "/" + text  # Slack DMs: "buy NVDA 180 5" (Slack reserves leading "/" for its own commands)
    parts = text.split()
    if not parts:
        return HELP
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    if cmd in ("/kr", "/국내"):
        if args and args[0].lstrip("/").lower() == "hold":  # 내 자산 lives in the main instance
            return handle(app, " ".join(args))
        kr = kr_app()
        if not kr:
            return "국내 시장 모드가 설정돼 있지 않아요"
        return handle(kr, " ".join(args) or "help")
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
        if cmd == "/mock":
            from . import live as LV
            return LV.slack_lines(LV.load(app.db)) or "모의투자가 아직 시작 전이에요"
        if cmd == "/playbook":
            from . import live as LV
            S = LV.stats(LV.load(app.db), app.backtest_expectancy())
            if not S:
                return "모의투자가 아직 시작 전이에요"
            ready = LV.readiness(S)
            out = [f"📓 직접 투자 준비도 {sum(r['ok'] for r in ready)}/{len(ready)}"]
            out += [f"{'✅' if r['ok'] else '⬜'} {r['label']} (지금 {r['now']})" for r in ready]
            note = (json.loads(app.db.get("playbook") or "null") or {}).get("note")
            if note:
                out += ["", f"*{note['headline']}*", "내가 직접 투자할 때 지킬 것"]
                out += [f"{i}. {r['rule']} — {r['why']}" for i, r in enumerate(note["my_rules"], 1)]
            else:
                out.append("\n첫 투자 노트는 토요일 주간 학습 때 생겨요")
            return "\n".join(out)
        if cmd == "/edge":
            return app.db.get("edge_summary") or "엣지 맵 없음 (주간 배치 후 생성)"
        if cmd == "/report":
            r = app.db.query("SELECT * FROM reports WHERE kind IN ('evening','morning','weekly') ORDER BY id DESC LIMIT 1")
            return r[0]["body"] if r else "리포트 없음"
        if cmd == "/hold":
            if not args:
                rows = app.db.query("SELECT * FROM holdings ORDER BY market, code")
                return "\n".join(f"{r['name'] or r['code']}({r['code']}) {r['qty']:g}주 평단 {r['avg_price']:,.2f}"
                                 for r in rows) or "등록된 자산 없음. 예: hold 379800 19 23965"
            if args[0] in ("rm", "del", "삭제") and len(args) > 1:
                app.db.execute("DELETE FROM holdings WHERE code=?", (args[1].upper(),))
                return f"삭제: {args[1].upper()}"
            if len(args) < 3:
                return "형식: hold 코드 수량 평단  (삭제: hold rm 코드)"
            code = args[0].upper()
            from . import quotes as Q
            name = (Q.get([code]).get(code) or {}).get("name") if Q.is_kr(code) else None
            app.db.set_holding(code, float(args[1].replace(",", "")), float(args[2].replace(",", "").lstrip("$")),
                               name, app.now().isoformat(timespec="seconds"))
            return f"저장: {name or code} {float(args[1]):g}주 평단 {float(args[2].replace(',', '')):,.2f}"
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
        raise SystemExit(f"{app.tg.name} 메신저 설정(토큰/ID 환경변수)이 필요합니다")
    if app.tg.name == "slack":
        return run_slack(app)
    return run_telegram(app)


def run_slack(app: App) -> None:
    from threading import Event

    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse

    sl = app.tg
    token = sl.current_app_token()
    if not token:
        raise SystemExit("SLACK_APP_TOKEN(xapp-... 또는 xoxe.xapp-...) 이 필요합니다 (Socket Mode)")
    allowed = str(sl.user_id)

    def process(client: SocketModeClient, req: SocketModeRequest) -> None:
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))  # ack within 3s
        p = req.payload
        if req.type == "slash_commands":
            user, channel, text = p.get("user_id"), p.get("channel_id"), p.get("text", "") or "help"
        elif req.type == "events_api":
            ev = p.get("event", {})
            if ev.get("type") != "message" or ev.get("bot_id") or ev.get("subtype"):
                return
            user, channel, text = ev.get("user"), ev.get("channel"), ev.get("text", "")
        else:
            return
        if str(user) != allowed:
            log.warning("ignored slack message from user %s", user)
            return
        try:
            sl.send(handle(app, text), channel=channel if req.type == "events_api" else None)
        except Exception:
            log.error("slack handler failed\n%s", traceback.format_exc())

    client = SocketModeClient(app_token=token, web_client=sl.web)
    client.socket_mode_request_listeners.append(process)
    client.connect()
    log.info("slack bot connected (socket mode)")
    stop = Event()
    while not stop.wait(600):  # rotating app tokens: refresh before expiry; reconnects use the new one
        client.app_token = sl.current_app_token() or client.app_token


def run_telegram(app: App) -> None:
    offset = int(app.db.get("tg_offset", "0") or 0) or None
    log.info("telegram bot started")
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
            if chat != str(app.tg.chat_id):
                log.warning("ignored message from chat %s", chat)
                continue
            text = msg.get("text") or ""
            if text:
                app.tg.send(handle(app, text))

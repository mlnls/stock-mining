"""Claude analyst: evening review, morning web briefing, weekly strategy research, ad-hoc questions.

Claude never places orders and never edits principles directly: rule ideas become proposals that
must pass the backtest gate and your approval in the messenger (Slack/Telegram).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import anthropic

from . import docfmt as DF
from . import labels as LB
from . import features as F
from . import rules as R

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

MARKET_DESC = {
    "us": "미국 주식(NYSE·NASDAQ, 한국에 사는 개인 투자자가 국내 증권사로 거래, 통화 USD)",
    "kr": "한국 주식(KOSPI·KOSDAQ, ISA 계좌에서 거래, 통화 KRW). 미국 시장의 결론을 그대로 가져오지 말고 국내 데이터로만 판단",
}

SYSTEM_BASE = """당신은 {market} 스윙 트레이딩 시스템의 분석가입니다.
사용자는 개인 투자자이며 최종 판단과 주문은 사용자가 직접 합니다.

역할
- 시스템이 계산한 신호·백테스트·페이퍼 트레이딩·매매일지 데이터를 해석하고, 리스크와 원칙 위반을 냉정하게 지적합니다.
- 숫자는 제공된 데이터만 사용합니다. 데이터에 없는 가격·실적·수치를 만들어내지 않습니다. 웹에서 찾은 정보는 출처를 짧게 밝힙니다.
- 과최적화를 경계합니다. 표본이 적은(30건 미만) 성과는 '아직 판단 불가'로 다룹니다.
- 원칙 수정은 '제안'만 합니다. 제안은 백테스트 게이트와 사용자 승인을 거쳐야 반영됩니다.
- 수익을 보장하거나 확신하는 표현을 쓰지 않습니다. 손실 가능성과 손절 원칙을 항상 우선합니다.

쓰는 방식 (사용자는 주식 초보입니다. 슬랙 폰 화면에서 10초 안에 읽혀야 합니다)
- 쉬운 한국어로 짧게. 한 줄에 한 가지만. 글머리표는 "• "만 쓰고, 제목(#)·굵게(**)·표·코드블록은 쓰지 않습니다.
- 전문 용어를 쓰지 않습니다: PF·기대값·가중치·표본 외·국면·페이퍼·블랙아웃·ATR·DSL 대신
  "이 방식이 과거에 벌었는지", "가상 매매", "실적 발표 직전" 같은 말로 풉니다.
- 전략은 영어 이름 대신 한글 이름으로 부릅니다: {strategy_names}
- 숫자는 꼭 필요한 것만, 한 문장에 하나. 데이터로 확인 못 한 내용은 길게 설명하지 말고 "확인 못 함" 한 마디로 끝냅니다."""

DSL_DOC = f"""규칙 명세(JSON) 형식:
{R.__doc__}
사용 가능한 지표:
{F.__doc__}
제약: 모든 규칙은 stop_atr 또는 stop_pct 손절을 가져야 하고 max_hold는 1~120입니다. 조건식에는 위 지표·숫자·함수만 쓸 수 있습니다.
신호는 장 마감 종가 기준으로 계산되고 다음 거래일에 진입합니다. 일봉(시고저종·거래량, value=종가×거래량)만 있으며
실적·재무·수급 데이터는 없습니다. value(거래대금)와 vavg_N의 단위는 해당 시장 통화입니다."""


def claude_bin() -> str | None:
    """`claude` on PATH, or the native installer's default location (~/.local/bin/claude)."""
    found = shutil.which("claude")
    if found:
        return found
    p = Path.home() / ".local" / "bin" / "claude"
    return str(p) if p.exists() else None


def backend(cfg) -> str:
    """api | cli | none (see config ``claude.backend``)."""
    b = cfg["claude"].get("backend", "auto")
    if b != "auto":
        return b
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    return "cli" if claude_bin() else "none"


def _call_cli(cfg, system: str, user: str, *, web: bool, schema: dict | None) -> str:
    """Claude Code headless (`claude -p`) on the user's Claude subscription. Runs in an empty temp dir with
    only web tools (or none), a replaced (small) system prompt, and the prompt on stdin."""
    exe = claude_bin()
    if not exe:
        raise RuntimeError("claude CLI not found (install Claude Code, then `claude setup-token`)")
    cmd = [exe, "-p", "--output-format", "json", "--system-prompt", system, "--max-turns", "40",
           "--tools", "WebSearch,WebFetch" if web else ""]
    if web:
        cmd += ["--allowedTools", "WebSearch", "WebFetch"]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    if cfg["claude"].get("cli_model"):
        cmd += ["--model", cfg["claude"]["cli_model"]]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # never fall back to paid API
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True, cwd=cwd, env=env, timeout=1800)
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-500:]}")
    if out.get("is_error"):
        raise RuntimeError(f"claude CLI error: {out.get('subtype')} {str(out.get('result'))[:300]}")
    log.info("claude-cli turns=%s duration=%ss", out.get("num_turns"), round(out.get("duration_ms", 0) / 1000))
    if schema:
        so = out.get("structured_output")
        if so is None:
            raise RuntimeError("claude CLI returned no structured output")
        return json.dumps(so, ensure_ascii=False)
    return (out.get("result") or "").strip()


def _client(cfg) -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=3, timeout=900)


def _call(cfg, system: str, user: str, effort: str, *, web: bool = False, schema: dict | None = None,
          max_tokens: int = 32000) -> str:
    """One Claude turn (continuing through pause_turn for server-side web search). Returns text."""
    if backend(cfg) == "cli":
        return _call_cli(cfg, system, user, web=web, schema=schema)
    client = _client(cfg)
    ccfg = cfg["claude"]
    kwargs: dict = {
        "model": ccfg["model"],
        "max_tokens": max_tokens,
        "system": system,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
    }
    if web:
        kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search",
                            "max_uses": ccfg["web_search_max_uses"],
                            "user_location": {"type": "approximate", "country": "US", "timezone": "America/New_York"}
                            if cfg.get("market") == "us" else
                            {"type": "approximate", "country": "KR", "timezone": "Asia/Seoul"}}]
    if schema:
        kwargs["output_config"] = {"effort": effort, "format": {"type": "json_schema", "schema": schema}}
    messages: list = [{"role": "user", "content": user}]
    for _ in range(6):
        with client.beta.messages.stream(messages=messages, **kwargs) as stream:
            msg = stream.get_final_message()
        log.info("claude %s stop=%s in=%s out=%s", msg._request_id, msg.stop_reason,
                 msg.usage.input_tokens, msg.usage.output_tokens)
        if msg.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": msg.content})
            continue
        if msg.stop_reason == "refusal":
            cat = msg.stop_details.category if msg.stop_details else None
            raise RuntimeError(f"Claude declined the request (category={cat})")
        return "".join(b.text for b in msg.content if b.type == "text").strip()
    raise RuntimeError("Claude turn did not finish after several pause_turn continuations")


def _system(cfg, principles: str) -> str:
    names = ", ".join(f"{k}={v}" for k, v in LB.STRATEGY_KO.items())
    base = SYSTEM_BASE.format(market=MARKET_DESC[cfg.get("market", "us")], strategy_names=names)
    return f"{base}\n\n<principles>\n{principles}\n</principles>"


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, indent=1)


# ---------------------------------------------------------------------------- evening
_INSIGHT = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["observation", "hypothesis", "lesson"]},
        "text": {"type": "string"},
    },
    "required": ["kind", "text"],
    "additionalProperties": False,
}
_INSIGHT_UPDATE = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "status": {"type": "string", "enum": ["open", "confirmed", "rejected"]},
        "note": {"type": "string"},
    },
    "required": ["id", "status", "note"],
    "additionalProperties": False,
}
_LEARNING_PROPS = {
    "new_insights": {"type": "array", "items": _INSIGHT},
    "insight_updates": {"type": "array", "items": _INSIGHT_UPDATE},
}

EVENING_SCHEMA = {
    "type": "object",
    "properties": {
        "comment": DF.DOC_SCHEMA,
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["principle", "rule"]},
                    "title": {"type": "string"},
                    "rationale": {"type": "string"},
                    "principle_text": {"type": "string"},
                    "rule_spec_json": {"type": "string"},
                },
                "required": ["kind", "title", "rationale", "principle_text", "rule_spec_json"],
                "additionalProperties": False,
            },
        },
        **_LEARNING_PROPS,
    },
    "required": ["comment", "proposals", "new_insights", "insight_updates"],
    "additionalProperties": False,
}


def evening_report(cfg, principles: str, ctx: dict) -> dict:
    user = f"""오늘 장 마감 후 데이터입니다.

<data>
{_dump(ctx)}
</data>

다음을 작성하세요.
1. comment: 저녁 코멘트. 후보의 가격·손절가·수량·금액은 시스템이 카드로 따로 보여 주므로 숫자를 반복하지 마세요.
   - headline: 내일에 대한 결론 한 문장
   - picks: 내일 매수 후보(recommendations)마다 decision=go, reason=왜 골랐는지 + 조심할 점 한 문장
   - sections(2~3개):
     🌡️ 시장 분위기 — 최근 한 달 흐름과 변동성을 쉬운 말로 1~2줄
     ⚠️ 조심할 점 — 보유 종목 경고(alerts), 실적 발표, 내 매매 습관 위반(discipline)이 있을 때만
     🧠 시스템 소식 — 전략이 쉬게 되거나 다시 켜졌는지 등 바뀐 게 있을 때만 1줄
   - sources: 빈 배열
2. proposals: 데이터가 뒷받침할 때만 0~2개. 근거가 약하면 빈 배열.
   - kind=principle: principle_text에 원칙.md에 추가할 한두 문장, rule_spec_json은 빈 문자열.
   - kind=rule: rule_spec_json에 아래 형식의 새 규칙 JSON 문자열(백테스트로 검증됨), principle_text는 빈 문자열.
3. new_insights: 학습 일지에 남길 새 관찰·가설·교훈 0~3개. 오늘 데이터(신호, 페이퍼 결과, edge_summary, 시장)에서
   '어떤 조건에서 돈이 벌리고/잃는가'에 대한 구체적이고 검증 가능한 문장으로. 이미 open_insights에 있는 내용은 반복하지 마세요.
   예) hypothesis: "VIX 20 이상일 때 qullamaggie_breakout 페이퍼 매매 손실이 몰린다 → 국면 필터 후보"
4. insight_updates: open_insights 중 오늘 데이터로 확인(confirmed)·기각(rejected)할 수 있는 것만. 근거를 note에.

{DF.DOC_GUIDE}

{DSL_DOC}"""
    text = _call(cfg, _system(cfg, principles), user, cfg["claude"]["effort_daily"], schema=EVENING_SCHEMA)
    return json.loads(text)


# ---------------------------------------------------------------------------- morning
MORNING_ITEMS = {
    "us": """- 프리마켓 선물(S&P500·나스닥), 간밤 아시아·유럽 증시, 미 국채 금리·달러·유가·VIX 등 오늘 장에 영향을 줄 요인
- 오늘 매수 후보(candidates)와 보유 종목(holdings) 각각의 최근 뉴스·SEC 공시(8-K, 유상증자, 전환사채), 애널리스트 등급 변경
- 실적 발표일: 후보 종목의 실적 발표가 앞으로 5거래일 안에 있으면 반드시 표시(스윙 보유 중 실적 갭 위험)
- 후보별로 '계획대로 진행' 또는 '보류 권고'와 짧은 이유
- 오늘 주의할 일정(CPI·고용·FOMC·옵션 만기, 주요 빅테크 실적)
- 한국 시간 기준 개장 시각도 한 줄로 알려주세요(서머타임 반영)""",
    "kr": """- 간밤 미국 증시(S&P500·나스닥·필라델피아 반도체), 환율·금리 등 오늘 한국 장에 영향을 줄 요인
- 오늘 매수 후보(candidates)와 보유 종목(holdings) 각각의 최근 뉴스·공시(유상증자, 전환사채, 실적 발표 일정, 소송, 거래정지 위험 등)
- 후보별로 '계획대로 진행' 또는 '보류 권고'와 짧은 이유
- 오늘 주의할 일정(경제지표, 옵션 만기, FOMC 등)""",
}


def morning_briefing(cfg, principles: str, ctx: dict) -> dict:
    market = cfg.get("market", "us")
    title = "미국 장 개장 전 브리핑" if market == "us" else "한국 장 시작 전 아침 브리핑"
    user = f"""{title}을 작성하세요. 웹 검색으로 최신 정보를 확인하세요.

<data>
{_dump(ctx)}
</data>

채울 내용:
- headline: 오늘의 결론 한 문장 (예: "REPL은 계획대로 진행해도 돼요" / "오늘 살 후보 없음")
- picks: 후보(candidates)·보유(holdings) 종목마다. 뉴스·공시·5거래일 안의 실적 발표를 보고 go/hold/watch
- sections: 🌡️ 시장 분위기(확인된 선물·금리·유가 등 2줄 이내) · 📅 오늘 일정(1~2줄) · ⏰ 개장 시각(한국 시간 1줄)
- sources: 확인에 쓴 기사
조사 항목 참고:
{MORNING_ITEMS[market]}

{DF.DOC_GUIDE}"""
    doc = json.loads(_call(cfg, _system(cfg, principles), user, cfg["claude"]["effort_daily"], web=True,
                           schema=DF.DOC_SCHEMA))
    avoid = {p["code"].upper(): p["reason"] for p in doc.get("picks", []) if p.get("decision") == "hold"}
    return {"doc": doc, "text": DF.to_slack(doc), "avoid": avoid}


# ---------------------------------------------------------------------------- weekly research
RESEARCH_TOPICS_US = [
    "Mark Minervini VCP·SEPA 실제 매수 규칙과 손절 원칙",
    "William O'Neil CAN SLIM과 컵앤핸들 돌파 규칙 (IBD 방식)",
    "Kristjan Kullamägi(Qullamaggie) 브레이크아웃·에피소딕 피벗·파라볼릭 숏 규칙",
    "Larry Connors 단기 평균회귀 전략들(RSI2, %b, 연속 하락일)과 S&P500 종목 대상 최신 검증",
    "Stan Weinstein 스테이지 분석(30주 이평) 진입 규칙",
    "Mark Minervini·David Ryan 등 US Investing Championship 우승자들의 공개 규칙",
    "학술 연구: 실적 발표 후 표류(PEAD)와 갭업 모멘텀, 일봉 가격만으로 근사하는 방법",
    "학술 연구: 52주 고점 모멘텀, 단기 반전, 산업 모멘텀의 미국 시장 최신 증거",
    "Linda Raschke·Larry Williams 단기 스윙 패턴(Holy Grail, 80-20, Turtle Soup)",
    "트레일링 스탑·시간 손절·부분 익절 등 청산 규칙 연구",
]

RESEARCH_TOPICS = [
    "Mark Minervini VCP·SEPA 실제 매수 규칙과 손절 원칙",
    "William O'Neil CAN SLIM과 컵앤핸들 돌파 규칙",
    "Kristjan Kullamägi(Qullamaggie) 브레이크아웃·에피소딕 피벗 규칙",
    "Larry Connors 단기 평균회귀 전략들(RSI2, %b, 연속 하락일)",
    "Stan Weinstein 스테이지 분석(30주 이평) 진입 규칙",
    "Nicolas Darvas 박스 이론의 기계적 규칙화",
    "한국 개인 슈퍼개미·전업 트레이더들이 공개한 스윙 매매 규칙(눌림목, 종가 베팅, 신고가 매매)",
    "변동성 돌파·래리 윌리엄스 전략의 한국 시장 적용 사례와 개선 필터",
    "학술 연구: 단기 반전(short-term reversal), 52주 고점 모멘텀, 거래량-수익률 관계의 한국 시장 증거",
    "트레일링 스탑·시간 손절·부분 익절 등 청산 규칙 연구",
]

RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "source": {"type": "string"},
                    "rationale": {"type": "string"},
                    "rule_spec_json": {"type": "string"},
                },
                "required": ["title", "source", "rationale", "rule_spec_json"],
                "additionalProperties": False,
            },
        },
        **_LEARNING_PROPS,
    },
    "required": ["candidates", "new_insights", "insight_updates"],
    "additionalProperties": False,
}


def research_rules(cfg, principles: str, ctx: dict, topic: str, n: int = 3) -> tuple[str, list[dict]]:
    """Step 1: web research on a topic. Step 2: turn findings into testable rule specs."""
    notes = _call(cfg, _system(cfg, principles), f"""이번 주 연구 주제: {topic}

웹 검색으로 유명 트레이더·연구자의 실제 규칙과 그 성과 근거(검증된 백테스트, 논문, 본인 공개 기록)를 조사하세요.
마케팅성 주장과 검증된 근거를 구분하고, 출처를 남기세요. 현재 시스템 현황은 아래와 같습니다.

<system_status>
{_dump(ctx)}
</system_status>

결과: 핵심 규칙 요약, 근거 수준, 우리 시스템(한국 일봉 데이터만 사용)에 옮길 수 있는 부분과 없는 부분.""",
                  cfg["claude"]["effort_research"], web=True)

    text = _call(cfg, _system(cfg, principles), f"""아래 리서치 노트를 바탕으로, 우리 시스템에서 백테스트할 새 규칙 후보를 최대 {n}개 만드세요.
기존 전략과 거의 같은 규칙은 피하고, 일봉 데이터로 표현할 수 없는 조건은 근사하되 근사했다고 rationale에 쓰세요.
name은 기존 전략 이름과 겹치지 않는 snake_case로 하세요. 기존 전략: {", ".join(ctx.get("strategy_names", []))}
과최적화를 피하려면 조건 수를 3~6개로 제한하고, param_grid는 핵심 파라미터 1~2개만 두세요.
시스템 현황의 open_insights(학습 일지의 미검증 가설)와 edge_map(전략별로 돈이 벌리고 잃는 조건)도 반영하세요.
가설을 규칙으로 바꿔 검증할 수 있으면 후보로 만들고, 리서치 결과로 판정되는 가설은 insight_updates에 기록하세요.
리서치에서 얻은 교훈은 new_insights(kind=lesson)로 남기세요.

<research_notes>
{notes}
</research_notes>

{DSL_DOC}""", cfg["claude"]["effort_research"], schema=RESEARCH_SCHEMA)
    return notes, json.loads(text)


# ---------------------------------------------------------------------------- ask
def ask(cfg, principles: str, ctx: dict, question: str) -> str:
    return _call(cfg, _system(cfg, principles), f"""<data>
{_dump(ctx)}
</data>

사용자 질문: {question}

필요하면 웹 검색을 하고, 1,200자 이내로 답하세요.""", cfg["claude"]["effort_daily"], web=True)

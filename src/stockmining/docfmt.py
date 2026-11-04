"""Structured report ("doc") shared by Slack and the web dashboard.

doc = {
  "headline": "한 줄 결론",
  "picks": [{"code": "REPL", "decision": "go" | "hold" | "watch", "reason": "..."}],
  "sections": [{"icon": "🌡️", "title": "시장 분위기", "items": ["...", "..."]}],
  "sources": [{"title": "...", "url": "https://..."}],
}
Claude fills it via a JSON schema; older plain-text reports are parsed into the same shape (``parse_text``).
"""
from __future__ import annotations

import re

DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "decision": {"type": "string", "enum": ["go", "hold", "watch"]},
                    "reason": {"type": "string"},
                },
                "required": ["code", "decision", "reason"],
                "additionalProperties": False,
            },
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "icon": {"type": "string"},
                    "title": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["icon", "title", "items"],
                "additionalProperties": False,
            },
        },
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                "required": ["title", "url"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "picks", "sections", "sources"],
    "additionalProperties": False,
}

DOC_GUIDE = """출력 형식(JSON)
- headline: 오늘의 결론 한 문장(40자 이내). 예) "REPL은 계획대로 사도 돼요", "오늘은 쉬는 날이에요"
- picks: 후보·보유 종목마다 하나. decision = go(계획대로) / hold(보류) / watch(지켜보기), reason = 이유 한 문장(60자 이내)
- sections: 2~4개. 각 섹션은 icon(이모지 1개), title(8자 이내), items(1~3개, 각 70자 이내의 쉬운 문장)
- sources: 웹에서 확인한 내용의 출처(최대 4개, 없으면 빈 배열). 본문에는 링크를 넣지 않습니다.
- 마크다운 기호(#, **, -, 표)를 쓰지 않습니다."""

DECISION = {"go": ("✅", "계획대로"), "hold": ("⛔", "보류"), "watch": ("👀", "지켜보기")}


def to_slack(doc: dict) -> str:
    out = []
    if doc.get("headline"):
        out.append(f"*{doc['headline']}*")
    for p in doc.get("picks", []):
        icon, label = DECISION.get(p.get("decision"), ("•", ""))
        out.append(f"{icon} *{p['code']}* {label} — {p.get('reason', '')}")
    for s in doc.get("sections", []):
        out.append("")
        out.append(f"{s.get('icon', '')} *{s.get('title', '')}*".strip())
        out += [f"• {i}" for i in s.get("items", [])]
    if doc.get("sources"):
        out.append("")
        out.append("출처 " + " · ".join(f"<{s['url']}|{s['title']}>" for s in doc["sources"][:4]))
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------- legacy text → doc
_HEAD = re.compile(r"^\s*(?:■|#{1,6}\s|\*\*(.+)\*\*\s*$|\*([^*]+)\*\s*$)")
_BULLET = re.compile(r"^\s*(?:[-•·▪]|\d+[.)])\s+")
_EMOJI = re.compile(r"^([\U0001F000-\U0001FAFF☀-➿⬀-⯿][️]?)\s*")
_MDLINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


_ICONS = [("시장", "🌡️"), ("국면", "🌡️"), ("일정", "📅"), ("후보", "🛒"), ("매수", "🛒"), ("보유", "📦"),
          ("포지션", "📦"), ("제외", "🚫"), ("원칙", "📏"), ("규율", "📏"), ("학습", "🧠"), ("전략", "🧠"),
          ("개장", "⏰"), ("시각", "⏰"), ("데이터", "ℹ️"), ("한계", "ℹ️"), ("제안", "🧪")]


def _icon_for(title: str) -> str:
    return next((i for k, i in _ICONS if k in title), "📌")


def parse_text(text: str) -> dict:
    """Best-effort: headings (■, #, *bold* line, emoji-led short line) start sections; bullets/lines are items.
    Markdown links become sources."""
    sources, seen = [], set()

    def strip_links(s: str) -> str:
        def repl(m):
            if m.group(2) not in seen:
                seen.add(m.group(2))
                sources.append({"title": m.group(1), "url": m.group(2)})
            return m.group(1)
        s = _MDLINK.sub(repl, s)
        s = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", s)
        return s.replace("**", "").strip()

    sections: list[dict] = []
    cur: dict | None = None
    lines = [l for l in text.splitlines()]
    # drop a leading title line like "📊 저녁 리포트 (11/03 마감)"
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and re.search(r"리포트|브리핑|학습", lines[0]) and len(lines[0]) < 50:
        lines.pop(0)
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        is_head = bool(_HEAD.match(line)) or (
            bool(_EMOJI.match(line.strip())) and len(line.strip()) <= 24 and not _BULLET.match(line))
        if is_head:
            t = re.sub(r"^\s*(?:■|#{1,6})\s*", "", line).strip().strip("*").strip()
            m = _EMOJI.match(t)
            icon, t = (m.group(1), t[m.end():]) if m else ("", t)
            t = strip_links(t)
            cur = {"icon": icon or _icon_for(t), "title": t, "items": []}
            sections.append(cur)
            continue
        item = strip_links(_BULLET.sub("", line))
        item = re.sub(r"^📝\s*(hypothesis|observation|lesson):\s*", "", item)
        if cur is None:
            cur = {"icon": "", "title": "", "items": []}
            sections.append(cur)
        if item:
            cur["items"].append(item)
    return {"headline": "", "picks": [], "sections": [s for s in sections if s["items"] or s["title"]],
            "sources": sources}

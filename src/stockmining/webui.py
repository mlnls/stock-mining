"""Dashboard building blocks: formatting, components, page shell (CSS + live-quote / chart script)."""
from __future__ import annotations

import json
import re
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from . import docfmt as DF


# ---------------------------------------------------------------------------- formatting
def h(v) -> str:
    return escape("" if v is None else str(v))


def money(v, cur: str = "USD", sign: bool = False) -> str:
    if v is None:
        return "-"
    s = ("+" if v > 0 else "-" if v < 0 else "") if sign else ("-" if v < 0 else "")
    return f"{s}${abs(v):,.2f}" if cur == "USD" else f"{s}{abs(v):,.0f}원"


def pct(v, digits: int = 2, sign: bool = True) -> str:
    if v is None:
        return "-"
    return f"{v:+.{digits}f}%" if sign else f"{v:.{digits}f}%"


def tone(v) -> str:
    """Korean convention: gain = red ("up"), loss = blue ("down")."""
    return "" if v is None or abs(v) < 1e-12 else ("up" if v > 0 else "down")


def shares(q) -> str:
    return "-" if q is None else f"{round(q, 4):g}주"


def kst(ts: str | None, fmt: str = "%-m월 %-d일 %H:%M") -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts).astimezone(ZoneInfo("Asia/Seoul")).strftime(fmt)
    except ValueError:
        return ts[:16].replace("T", " ")


# ---------------------------------------------------------------------------- live quote spans
def live(code: str, kind: str, q: dict | None, cur: str, qty: float | None = None, avg: float | None = None,
         ref: float | None = None) -> str:
    """A span the page script keeps current. kind: price | chg | val | pnl | vs."""
    text, t = "-", ""
    p = (q or {}).get("price")
    if p is not None:
        if kind == "price":
            text = money(p, cur)
        elif kind == "chg":
            text, t = f'{money(q.get("change"), cur, True)} ({pct(q.get("change_pct"))})', tone(q.get("change"))
        elif kind == "val" and qty is not None:
            text = money(p * qty, cur)
        elif kind == "pnl" and qty is not None and avg:
            d = (p - avg) * qty
            text, t = f"{money(d, cur, True)} ({pct((p / avg - 1) * 100)})", tone(d)
        elif kind == "vs" and ref:
            text, t = pct((p / ref - 1) * 100), tone(p - ref)
    attrs = f'data-q="{h(code)}" data-k="{kind}" data-cur="{cur}"'
    if qty is not None:
        attrs += f' data-qty="{qty}"'
    if avg:
        attrs += f' data-avg="{avg}"'
    if ref:
        attrs += f' data-ref="{ref}"'
    return f'<span class="lv {t}" {attrs}>{text}</span>'


def market_state(q: dict | None) -> str:
    if not q:
        return '<span class="state">시세 없음</span>'
    when = kst(q.get("time"), "%H:%M") if q.get("time") else ""
    return (f'<span class="state {"on" if q.get("open") else ""}">'
            f'{"● 장중" if q.get("open") else "장 마감"}{" · " + when if when else ""}</span>')


# ---------------------------------------------------------------------------- components
def avatar(label: str, kind: str = "") -> str:
    return f'<span class="av {kind}">{h(label[:4])}</span>'


def link_row(href: str, av: str, title: str, sub: str, right: str, right_sub: str = "") -> str:
    return (f'<a class="row" href="{h(href)}">{av}<span class="rt"><b>{title}</b><small>{sub}</small></span>'
            f'<span class="rr"><b>{right}</b><small>{right_sub}</small></span><i class="go"></i></a>')


def fold_row(av: str, title: str, sub: str, right: str, right_sub: str, body: str, open_: bool = False) -> str:
    return (f'<details class="row fold-row"{" open" if open_ else ""}><summary>{av}'
            f'<span class="rt"><b>{title}</b><small>{sub}</small></span>'
            f'<span class="rr"><b>{right}</b><small>{right_sub}</small></span><i class="chev"></i></summary>'
            f'<div class="rb">{body}</div></details>')


def kv(items: list[tuple[str, str]]) -> str:
    return '<dl class="kv">' + "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in items) + "</dl>"


def card(title: str, body: str, more: tuple[str, str] | None = None, count: str = "") -> str:
    head = f'<h2>{title}{f" <small>{count}</small>" if count else ""}'
    if more:
        head += f'<a class="more" href="{h(more[0])}">{more[1]}</a>'
    return f'<section class="card">{head}</h2>{body}</section>'


def empty(icon: str, text: str) -> str:
    return f'<div class="empty"><span>{icon}</span>{text}</div>'


def chips(items: list[tuple[str, str, bool]]) -> str:
    """Sub-navigation pills: (href, label, active)."""
    return '<div class="pills">' + "".join(
        f'<a href="{h(u)}" class="{"on" if on else ""}">{t}</a>' for u, t, on in items) + "</div>"


def chart(points: list[tuple[str, float]], cur: str = "USD", lines: dict[str, float | None] | None = None,
          height: int = 180) -> str:
    if len(points) < 2:
        return ""
    data = {"d": [p[0] for p in points], "c": [round(p[1], 4) for p in points], "cur": cur,
            "lines": {k: v for k, v in (lines or {}).items() if v}}
    return f'<div class="chart" style="height:{height}px" data-chart="{h(json.dumps(data))}"></div>'


def inline(text: str) -> str:
    t = h(text)
    t = re.sub(r"&lt;(https?://[^|&]+)\|([^&]+)&gt;", r'<a href="\1" target="_blank" rel="noopener">\2</a>', t)
    return re.sub(r"\*(\S[^*\n]*?)\*", r"<b>\1</b>", t)


def doc_html(doc: dict, learned: list[str] | None = None) -> str:
    out = []
    if doc.get("headline"):
        out.append(f'<div class="callout">{inline(doc["headline"])}</div>')
    if doc.get("picks"):
        rows = ""
        for p in doc["picks"]:
            icon, label = DF.DECISION.get(p.get("decision"), ("•", ""))
            rows += (f'<li><span class="av">{h(p["code"][:4])}</span><span class="rt"><b>'
                     f'<a href="/stock/{h(p["code"])}">{h(p["code"])}</a> <em class="d-{h(p.get("decision"))}">{icon} {label}</em>'
                     f'</b><span class="why">{inline(p.get("reason", ""))}</span></span></li>')
        out.append(f'<ul class="picks">{rows}</ul>')
    secs = list(doc.get("sections", []))
    if learned:
        secs.append({"icon": "🧠", "title": "새로 배운 것", "items": learned})
    for sec in secs:
        items = "".join(f"<li>{inline(i)}</li>" for i in sec.get("items", []))
        head = (f'<div class="sh"><span class="si">{h(sec.get("icon") or "•")}</span>{inline(sec.get("title", ""))}</div>'
                if sec.get("title") or sec.get("icon") else "")
        out.append(f'<div class="sec">{head}<ul>{items}</ul></div>')
    if doc.get("sources"):
        links = "".join(f'<a href="{h(x["url"])}" target="_blank" rel="noopener">{h(x["title"][:40])}</a>'
                        for x in doc["sources"][:6] if str(x.get("url", "")).startswith("http"))
        out.append(f'<div class="src"><span>출처</span>{links}</div>')
    return '<div class="doc">' + "".join(out) + "</div>"


def report_doc(r: dict) -> dict:
    data = json.loads(r["data"]) if r.get("data") else {}
    return data if data.get("doc") else {"doc": DF.parse_text(r["body"])}


# ---------------------------------------------------------------------------- strategy rules in plain Korean
_RULE_WORDS = [
    (r"prev\((\w+),\s*(\d+)\)", r"\2일 전 \1"),
    (r"\bxrank_ret_(\d+)", r"\1일 상승률 순위(0~100)"),
    (r"\bxrank_(\w+)", r"\1 순위(0~100)"),
    (r"\bsma_(\d+)", r"\1일 평균선"), (r"\bema_(\d+)", r"\1일 지수평균선"),
    (r"\bhh_(\d+)", r"직전 \1일 최고가"), (r"\bll_(\d+)", r"직전 \1일 최저가"),
    (r"\bhc_(\d+)", r"직전 \1일 최고 종가"), (r"\blc_(\d+)", r"직전 \1일 최저 종가"),
    (r"\brsi_(\d+)", r"RSI(\1일)"), (r"\bnatr_(\d+)", r"하루 변동폭%(\1일 평균)"), (r"\batr_(\d+)", r"하루 변동폭(\1일)"),
    (r"\bret_(\d+)", r"\1일 수익률%"), (r"\bvol_ratio_(\d+)", r"거래량 배수(\1일 평균 대비)"),
    (r"\bvma_(\d+)", r"\1일 평균 거래량"), (r"\bvavg_(\d+)", r"\1일 평균 거래대금"),
    (r"\bbbu_(\d+)", r"볼린저 상단(\1일)"), (r"\bbbl_(\d+)", r"볼린저 하단(\1일)"), (r"\bbbw_(\d+)", r"볼린저 폭%(\1일)"),
    (r"\bup_days_(\d+)", r"최근 \1일 중 오른 날 수"), (r"\bml_score\b", "AI 점수(0~100)"),
    (r"\bclose\b", "종가"), (r"\bopen\b", "시가"), (r"\bhigh\b", "고가"), (r"\blow\b", "저가"),
    (r"\bvolume\b", "거래량"), (r"\bvalue\b", "거래대금"), (r"\bgap\b", "시가 갭%"),
    (r"\band\b", "그리고"), (r"\bor\b", "또는"), (r">=", "≥"), (r"<=", "≤"),
]


def explain(expr: str) -> str:
    t = expr
    for pat, rep in _RULE_WORDS:
        t = re.sub(pat, rep, t)
    return t


def explain_exit(ex: dict) -> list[str]:
    out = []
    if ex.get("stop_atr"):
        out.append(f"손절: 산 가격에서 하루 변동폭의 {ex['stop_atr']:g}배만큼 내려가면 판다")
    if ex.get("stop_pct"):
        out.append(f"손절: 산 가격에서 {ex['stop_pct']:g}% 내려가면 판다")
    if ex.get("target_atr"):
        out.append(f"익절: 하루 변동폭의 {ex['target_atr']:g}배만큼 오르면 판다")
    if ex.get("target_pct"):
        out.append(f"익절: {ex['target_pct']:g}% 오르면 판다")
    if ex.get("trail_atr"):
        out.append(f"따라 올라가는 손절: 최고 종가에서 변동폭 {ex['trail_atr']:g}배 내려오면 판다")
    for e in ex.get("exit_when") or []:
        out.append(f"{explain(e)} 이면 다음 날 시가에 판다")
    if ex.get("max_hold"):
        out.append(f"아무 일 없어도 최대 {int(ex['max_hold'])}거래일 뒤에는 판다")
    return out


# ---------------------------------------------------------------------------- shell
TABS = [("/", "홈", "M3 11.5 12 4l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"),
        ("/assets", "내 자산", "M4 7h16v12H4zM4 7l2-3h12l2 3M15 13h2"),
        ("/picks", "추천", "M12 3l2.6 5.6 6.1.7-4.5 4.2 1.2 6L12 16.6 6.6 19.5l1.2-6L3.3 9.3l6.1-.7z"),
        ("/strategies", "전략", "M4 19V9M10 19V5M16 19v-7M22 19H2"),
        ("/sim", "모의투자", "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM10 8.5v7l6-3.5z"),
        ("/reports", "리포트", "M6 3h9l4 4v14H6zM9 12h7M9 16h7M9 8h3")]


def page(title: str, body: str, tab: str, back: str | None = None, quote_codes: list[str] | None = None) -> str:
    nav = "".join(
        f'<a href="{u}" class="{"on" if u == tab else ""}"><svg viewBox="0 0 24 24"><path d="{d}"/></svg>{t}</a>'
        for u, t, d in TABS)
    top = (f'<header class="top">{f"<a class=back href={h(back)} aria-label=뒤로></a>" if back else ""}'
           f'<h1>{h(title)}</h1></header>')
    codes = ",".join(dict.fromkeys(quote_codes or []))
    return (SHELL.replace("{{title}}", h(title)).replace("{{top}}", top).replace("{{body}}", body)
            .replace("{{nav}}", nav).replace("{{codes}}", h(codes)))


SHELL = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow"><meta name="theme-color" content="#f2f4f6"><title>{{title}} · Stock Mining</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<style>
:root{color-scheme:light;--bg:#f2f4f6;--card:#fff;--line:#eef0f3;--ink:#191f28;--ink2:#4e5968;--ink3:#8b95a1;--fill:#f2f4f6;
--accent:#3182f6;--up:#f04452;--down:#3182f6;--upbg:#fdeef0;--downbg:#e8f3ff;--warn:#c27a00;--warnbg:#fff4e0}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#101013;--card:#1c1c21;--line:#2a2a31;--ink:#f2f3f5;--ink2:#b0b8c1;
--ink3:#7e8792;--fill:#2a2a31;--accent:#4c94ff;--up:#ff5c6a;--down:#4c94ff;--upbg:#3a1f24;--downbg:#1a2a44;--warn:#ffb340;--warnbg:#3a2a10}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Pretendard Variable",Pretendard,-apple-system,BlinkMacSystemFont,
"Apple SD Gothic Neo","Noto Sans KR",sans-serif;-webkit-font-smoothing:antialiased;letter-spacing:-.01em;
padding-bottom:calc(76px + env(safe-area-inset-bottom))}
a{color:inherit;text-decoration:none}
.wrap{max-width:600px;margin:0 auto;padding:0 12px 24px}
.top{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(10px);
display:flex;align-items:center;gap:6px;max-width:600px;margin:0 auto;padding:12px 16px 8px}
.top h1{font-size:22px;font-weight:700;margin:0;letter-spacing:-.02em}
.back{width:32px;height:32px;display:grid;place-items:center;margin-left:-8px}
.back::before{content:"";width:11px;height:11px;border-left:2.5px solid var(--ink);border-bottom:2.5px solid var(--ink);transform:rotate(45deg);margin-left:4px}
nav.tabs{position:fixed;left:0;right:0;bottom:0;z-index:6;background:var(--card);border-top:1px solid var(--line);
display:flex;justify-content:center;padding:6px 0 calc(6px + env(safe-area-inset-bottom))}
nav.tabs a{flex:1;max-width:120px;display:flex;flex-direction:column;align-items:center;gap:2px;font-size:11.5px;font-weight:600;color:var(--ink3)}
nav.tabs svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linejoin:round;stroke-linecap:round}
nav.tabs a.on{color:var(--ink)}
.card,.hero{background:var(--card);border-radius:20px;padding:20px;margin:12px 0}
.hero small{color:var(--ink3);font-size:13px}.hero .lbl{display:block;color:var(--ink2);font-size:15px;margin-top:6px}
.big{display:block;font-size:34px;font-weight:700;letter-spacing:-.02em;margin:2px 0 4px;font-variant-numeric:tabular-nums}
.hero p{margin:0;font-size:15px;font-weight:600;color:var(--ink2)}
h2{font-size:19px;font-weight:700;margin:0 0 10px;display:flex;align-items:baseline;gap:4px}
h2 small{color:var(--accent);font-size:15px;font-weight:600}
h2 .more{margin-left:auto;font-size:14px;font-weight:500;color:var(--ink3)}h2 .more::after{content:" ›"}
h3{font-size:15px;color:var(--ink2);margin:20px 0 4px}
.up{color:var(--up)!important}.down{color:var(--down)!important}
.lead{color:var(--ink3);font-size:14px;margin:-4px 0 6px}
.mkt{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.mkt a{background:var(--card);border-radius:16px;padding:12px 14px;display:flex;flex-direction:column;gap:1px;min-width:0}
.mkt span{font-size:13px;color:var(--ink2)}.mkt b{font-size:16px;font-variant-numeric:tabular-nums}.mkt small{font-size:12.5px;font-weight:600}
.row{display:flex;align-items:center;gap:12px;padding:12px 8px;margin:0 -8px;border-radius:14px;cursor:pointer}
a.row:hover,.fold-row>summary:hover{background:var(--fill)}
.fold-row{display:block;padding:0}.fold-row[open]{background:var(--fill)}
.fold-row>summary{list-style:none;display:flex;align-items:center;gap:12px;padding:12px 8px;border-radius:14px}
.fold-row>summary::-webkit-details-marker{display:none}
.av{flex:0 0 42px;height:42px;border-radius:50%;background:var(--fill);display:grid;place-items:center;font-size:11.5px;font-weight:700;
color:var(--ink2);letter-spacing:-.03em;overflow:hidden}
.av.etf{background:#1b3a6b;color:#fff}.fold-row[open] .av{background:var(--card)}.fold-row[open] .av.etf{background:#1b3a6b}
.rt{flex:1;min-width:0;display:flex;flex-direction:column}.rt b{font-size:16px;font-weight:600;overflow:hidden;text-overflow:ellipsis}
.rt small,.rr small{color:var(--ink3);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rr{display:flex;flex-direction:column;align-items:flex-end;font-variant-numeric:tabular-nums;text-align:right}
.rr b{font-size:16px;font-weight:600;white-space:nowrap}.rr small{font-weight:500}
.nm{color:var(--ink3);font-weight:400;font-size:13px;margin-left:2px}
.go{width:7px;height:7px;border-right:2px solid var(--ink3);border-top:2px solid var(--ink3);transform:rotate(45deg);margin:0 4px 0 0;flex:0 0 7px}
.chev{width:8px;height:8px;border-right:2px solid var(--ink3);border-bottom:2px solid var(--ink3);transform:rotate(45deg);margin:0 4px 4px 2px;
transition:transform .2s}details[open]>summary .chev{transform:rotate(-135deg);margin-bottom:-4px}
.rb{padding:2px 12px 16px}
em{font-style:normal;font-size:12px;font-weight:600;padding:2px 7px;border-radius:6px;background:var(--fill);color:var(--ink2);margin-right:4px;white-space:nowrap}
.fold-row[open] em{background:var(--card)}em.good{background:var(--upbg);color:var(--up)}em.bad{background:var(--downbg);color:var(--down)}
em.warn{background:var(--warnbg);color:var(--warn)}
.kv{margin:6px 0 0;display:grid}.kv div{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--line)}
.kv div:last-child{border:0}.kv dt{color:var(--ink2)}.kv dd{margin:0;font-weight:600;text-align:right;font-variant-numeric:tabular-nums}
.kv small{color:var(--ink3);font-weight:400;font-size:13px}.fold-row[open] .kv div{border-color:color-mix(in srgb,var(--ink3) 18%,transparent)}
.hint,.desc{color:var(--ink2);font-size:14.5px;margin:12px 0 0;line-height:1.6}.hero p.desc,.desc{margin:6px 0 0;font-weight:400}.hero p.lead{font-weight:400}
code{background:var(--fill);padding:1px 6px;border-radius:6px;font-size:13px;white-space:nowrap}
.state{font-size:13px;color:var(--ink3);font-weight:500}.state.on{color:var(--up)}
.quote{display:flex;flex-direction:column;gap:2px}.quote .big{margin:0}.quote .chg{font-size:16px;font-weight:600}
.pills{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;margin:4px 0 2px}.pills::-webkit-scrollbar{display:none}
.pills a{white-space:nowrap;padding:7px 13px;border-radius:999px;font-size:14px;font-weight:600;background:var(--card);color:var(--ink2)}
.pills a.on{background:var(--ink);color:var(--card)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}.stat{background:var(--fill);border-radius:14px;padding:12px 14px}
.stat span{display:block;font-size:13px;color:var(--ink2)}.stat b{font-size:18px;font-variant-numeric:tabular-nums}
.stat small{display:block;color:var(--ink3);font-size:12.5px}
.steps{list-style:none;margin:6px 0 0;padding:0;counter-reset:s}.steps li{counter-increment:s;position:relative;padding:9px 0 9px 34px;
border-bottom:1px solid var(--line);font-size:15px;line-height:1.55}.steps li:last-child{border:0}
.steps li::before{content:counter(s);position:absolute;left:0;top:9px;width:24px;height:24px;border-radius:50%;background:var(--fill);
display:grid;place-items:center;font-size:12px;font-weight:700;color:var(--ink2)}
.steps code{display:block;width:fit-content;margin-top:4px;white-space:normal;color:var(--ink3);font-size:12px}
table.t{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:14px}
table.t th{font-weight:500;color:var(--ink3);text-align:right;padding:6px 4px;font-size:12.5px}table.t th:first-child,table.t td:first-child{text-align:left}
table.t td{padding:9px 4px;text-align:right;border-top:1px solid var(--line)}
.chart{position:relative;margin:4px 0 6px;touch-action:pan-y}.chart svg{display:block;width:100%;height:100%;overflow:visible}
.chart .ln{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.chart .ref{stroke:var(--ink3);stroke-width:1;stroke-dasharray:4 4}.chart .reflbl{fill:var(--ink3);font-size:11px}
.chart .cross{stroke:var(--ink3);stroke-width:1}.chart .dot{stroke:var(--card);stroke-width:2}
.tip{position:absolute;top:-6px;transform:translateX(-50%);background:var(--ink);color:var(--card);font-size:12px;font-weight:600;
padding:4px 8px;border-radius:8px;white-space:nowrap;pointer-events:none;font-variant-numeric:tabular-nums}
.empty{display:flex;flex-direction:column;align-items:center;gap:6px;padding:22px 8px;color:var(--ink3);text-align:center;font-size:14px}
.empty span{font-size:30px}
.alert{background:var(--warnbg);color:var(--warn);border-radius:14px;padding:12px 14px;margin:10px 0;font-size:14px;font-weight:500}
.doc{display:flex;flex-direction:column;gap:10px}
.callout{background:var(--downbg);color:var(--ink);border-radius:16px;padding:16px 18px;font-size:18px;font-weight:700;line-height:1.45}
.picks{list-style:none;margin:0;background:var(--fill);border-radius:16px;padding:4px 14px}
.picks li{display:flex;gap:12px;align-items:flex-start;padding:12px 0;border-bottom:1px solid color-mix(in srgb,var(--ink3) 18%,transparent)}
.picks li:last-child{border:0}.picks .av{background:var(--card)}.picks .why{color:var(--ink2);font-size:14.5px;line-height:1.55;margin-top:2px}
em.d-go{background:var(--upbg);color:var(--up)}em.d-hold{background:var(--warnbg);color:var(--warn)}em.d-watch{background:var(--card)}
.sec{background:var(--fill);border-radius:16px;padding:14px 16px}
.sh{display:flex;align-items:center;gap:8px;font-weight:700;font-size:16px;margin-bottom:6px}
.si{width:30px;height:30px;border-radius:10px;background:var(--card);display:grid;place-items:center;font-size:16px}
.sec ul{list-style:none;margin:0;padding:0}.sec li{position:relative;padding:7px 0 7px 14px;font-size:15px;line-height:1.6;word-break:keep-all;overflow-wrap:anywhere}
.sec li::before{content:"";position:absolute;left:2px;top:16px;width:4px;height:4px;border-radius:50%;background:var(--ink3)}
.sec li+li{border-top:1px solid color-mix(in srgb,var(--ink3) 14%,transparent)}
.src{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:13px}.src span{color:var(--ink3)}
.src a{background:var(--fill);color:var(--ink2);padding:5px 10px;border-radius:999px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.plain{list-style:none;margin:0;padding:0}.plain li.note{border-bottom:1px solid var(--line);padding:12px 0}.plain li.note:last-child{border:0}
.plain li.note small{display:block;color:var(--ink3);font-size:13px;margin-top:3px}
.updated{text-align:center;color:var(--ink3);font-size:12px;margin:8px 0}
footer{text-align:center;color:var(--ink3);font-size:12px;padding:8px 16px 16px}
</style></head><body>
{{top}}
<div class="wrap">{{body}}<p class="updated" id="upd"></p></div>
<footer>보기 전용 · 기록과 승인은 슬랙에서 · 투자 판단과 주문은 직접 하세요</footer>
<nav class="tabs">{{nav}}</nav>
<script>
(function(){
var CODES="{{codes}}",NS="http://www.w3.org/2000/svg";
function money(v,cur,sign){if(v==null||isNaN(v))return"-";var s=sign?(v>0?"+":v<0?"-":""):(v<0?"-":"");var a=Math.abs(v);
  return cur==="USD"?s+"$"+a.toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2}):s+Math.round(a).toLocaleString("ko-KR")+"원"}
function pct(v){return v==null||isNaN(v)?"-":(v>0?"+":"")+v.toFixed(2)+"%"}
function tone(e,v){e.classList.remove("up","down");if(v>0)e.classList.add("up");else if(v<0)e.classList.add("down")}
function apply(Q){
  document.querySelectorAll("[data-q]").forEach(function(e){var q=Q[e.dataset.q];if(!q||q.price==null)return;
    var p=q.price,cur=e.dataset.cur,k=e.dataset.k,qty=+e.dataset.qty,avg=+e.dataset.avg,ref=+e.dataset.ref;
    if(k==="price")e.textContent=money(p,cur);
    else if(k==="chg"){e.textContent=money(q.change,cur,true)+" ("+pct(q.change_pct)+")";tone(e,q.change)}
    else if(k==="val")e.textContent=money(p*qty,cur);
    else if(k==="pnl"&&avg){var d=(p-avg)*qty;e.textContent=money(d,cur,true)+" ("+pct((p/avg-1)*100)+")";tone(e,d)}
    else if(k==="vs"&&ref){e.textContent=pct((p/ref-1)*100);tone(e,p-ref)}});
  var tot=document.querySelector("[data-total]");if(tot){var fx=(Q["KRW=X"]||{}).price||+tot.dataset.fx,v=0,c=0,day=0;
    document.querySelectorAll("[data-hold]").forEach(function(r){var q=Q[r.dataset.hold],m=r.dataset.cur==="USD"?fx:1,qty=+r.dataset.qty;
      var p=q&&q.price!=null?q.price:+r.dataset.last;v+=p*qty*m;c+=+r.dataset.avg*qty*m;if(q&&q.change!=null)day+=q.change*qty*m});
    tot.textContent=money(v,"KRW");var pl=document.querySelector("[data-total-pnl]");if(pl){pl.textContent=money(v-c,"KRW",true)+" ("+pct(c?(v/c-1)*100:0)+")";tone(pl,v-c)}
    var dy=document.querySelector("[data-total-day]");if(dy){dy.textContent=money(day,"KRW",true);tone(dy,day)}}
}
function poll(){if(!CODES||document.hidden)return;fetch("/api/quotes?c="+encodeURIComponent(CODES),{credentials:"same-origin"})
  .then(function(r){return r.json()}).then(function(Q){apply(Q);var u=document.getElementById("upd");
  if(u)u.textContent="시세 "+new Date().toLocaleTimeString("ko-KR",{hour:"2-digit",minute:"2-digit",second:"2-digit"})+" 기준 · 20초마다 갱신"}).catch(function(){})}
if(CODES){poll();setInterval(poll,20000);document.addEventListener("visibilitychange",poll)}
function el(t,a){var e=document.createElementNS(NS,t);for(var k in a)e.setAttribute(k,a[k]);return e}
var NAMES={stop:"손절",avg:"평단",entry:"기준가",target:"목표"};
function draw(box){
  if(box.dataset.done||!box.offsetWidth)return;box.dataset.done=1;
  var D=JSON.parse(box.dataset.chart),c=D.c,W=box.offsetWidth,H=box.offsetHeight,pad=6,lab=Object.keys(D.lines).length?70:8;
  var vals=c.slice();for(var k in D.lines)vals.push(D.lines[k]);
  var lo=Math.min.apply(null,vals),hi=Math.max.apply(null,vals),sp=(hi-lo)||1;lo-=sp*.06;hi+=sp*.06;sp=hi-lo;
  var X=function(i){return pad+i/(c.length-1)*(W-pad*2-lab)},Y=function(v){return pad+(hi-v)/sp*(H-pad*2)};
  var col=getComputedStyle(document.documentElement).getPropertyValue(c[c.length-1]>=c[0]?"--up":"--down").trim();
  var svg=el("svg",{viewBox:"0 0 "+W+" "+H,role:"img","aria-label":"최근 "+c.length+"일 종가, 마지막 "+money(c[c.length-1],D.cur)});
  for(var n in D.lines){var v=D.lines[n];svg.appendChild(el("line",{x1:pad,x2:W-pad-lab+4,y1:Y(v),y2:Y(v),"class":"ref"}));
    var t=el("text",{x:W-pad-lab+8,y:Y(v)+4,"class":"reflbl"});t.textContent=(NAMES[n]||n)+" "+money(v,D.cur);svg.appendChild(t)}
  svg.appendChild(el("path",{d:c.map(function(v,i){return(i?"L":"M")+X(i).toFixed(1)+" "+Y(v).toFixed(1)}).join(""),"class":"ln",stroke:col}));
  svg.appendChild(el("circle",{cx:X(c.length-1),cy:Y(c[c.length-1]),r:4,fill:col,"class":"dot"}));
  var cross=el("line",{y1:0,y2:H,"class":"cross",visibility:"hidden"}),dot=el("circle",{r:5,fill:col,"class":"dot",visibility:"hidden"});
  svg.appendChild(cross);svg.appendChild(dot);box.appendChild(svg);
  var tip=document.createElement("div");tip.className="tip";tip.style.display="none";box.appendChild(tip);
  function show(e){var r=box.getBoundingClientRect(),i=Math.round((e.clientX-r.left-pad)/(W-pad*2-lab)*(c.length-1));
    i=Math.max(0,Math.min(c.length-1,i));var px=X(i);cross.setAttribute("x1",px);cross.setAttribute("x2",px);dot.setAttribute("cx",px);
    dot.setAttribute("cy",Y(c[i]));cross.setAttribute("visibility","visible");dot.setAttribute("visibility","visible");
    tip.textContent=D.d[i].slice(2).replace(/-/g,".")+"  "+money(c[i],D.cur);tip.style.display="block";tip.style.left=Math.max(55,Math.min(W-55,px))+"px"}
  function hide(){cross.setAttribute("visibility","hidden");dot.setAttribute("visibility","hidden");tip.style.display="none"}
  box.addEventListener("pointermove",show);box.addEventListener("pointerdown",show);box.addEventListener("pointerleave",hide)}
function charts(){document.querySelectorAll(".chart").forEach(draw)}
document.querySelectorAll("details").forEach(function(d){d.addEventListener("toggle",charts)});charts();
})();
</script>
</body></html>"""

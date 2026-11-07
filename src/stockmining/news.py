"""News and disclosures as data: headlines collected every evening, tagged and scored, stored with the time
they were SEEN so that anything learned from them later only uses what was known at that moment.

Sources (no key needed unless noted):
  * market:  Google News RSS (KR: 코스피·증시·금리·환율 / US: stock market·Fed·Nasdaq)
  * stocks:  KR Google News RSS by company name · US Yahoo Finance RSS by ticker
  * KR disclosures: DART (OpenDART, free key in env DART_API_KEY) — 유상증자·CB·감자 etc.
Scoring: Claude labels each new headline (sentiment −2..+2, event tags) in one batch; without Claude a keyword
rule does a rougher job. Rule tags for events are always applied.

There is no news history for the past four years, so news can't be backtested; its value is measured going
forward (the live mock ledger records each buy's news state and the weekly note compares outcomes).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (compatible; stockmining-news/1.0)"}
MAX_PER_FEED = 15
MAX_CODES = 60

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,            -- '' = market-wide
    kind TEXT NOT NULL,            -- news | disclosure
    published TEXT,
    seen TEXT NOT NULL,            -- when we fetched it (point-in-time key)
    title TEXT NOT NULL,
    source TEXT,
    link TEXT,
    tags TEXT,                     -- JSON list
    sentiment REAL,                -- -2..+2 (NULL = not scored yet)
    scorer TEXT,                   -- claude | rule
    UNIQUE(code, title)
);
CREATE INDEX IF NOT EXISTS news_code_seen ON news(code, seen);
"""

MARKET_Q = {"kr": ["코스피", "코스닥", "증시", "기준금리", "원달러 환율"],
            "us": ["stock market", "S&P 500", "Federal Reserve", "Nasdaq"]}

# event tags by keyword (always applied; Claude may add more)
TAGS = {
    "capital_raise": r"유상증자|전환사채|신주인수권|\bCB\b|\bBW\b|offering|dilut",
    "capital_cut": r"감자|reverse split",
    "earnings": r"실적|영업이익|순이익|매출|어닝|earnings|guidance|revenue|EPS",
    "contract": r"수주|공급계약|계약 체결|contract|deal|partnership",
    "legal": r"소송|횡령|배임|압수수색|검찰|과징금|lawsuit|probe|investigation|SEC charges|fraud",
    "rating": r"목표가|투자의견|상향|하향|upgrade|downgrade|price target",
    "buyback": r"자사주|자기주식|buyback|repurchase",
    "mna": r"인수|합병|M&A|acquir|merger|takeover",
    "delisting": r"상장폐지|거래정지|관리종목|투자경고|delist|halt",
    "macro": r"금리|환율|물가|CPI|Fed|연준|고용|inflation|tariff|관세",
}
NEG_TAGS = {"capital_raise", "capital_cut", "legal", "delisting"}
POS_W = r"급등|상승|최대|호실적|흑자|수주|상향|신고가|surge|jump|beat|record|upgrade|soar|rally"
NEG_W = r"급락|하락|적자|부진|하향|우려|쇼크|폭락|plunge|drop|miss|downgrade|fall|slump|cut"


def ensure(db) -> None:
    db.conn.executescript(SCHEMA_SQL)
    db.conn.commit()


def rule_tags(title: str) -> list[str]:
    return [k for k, pat in TAGS.items() if re.search(pat, title, re.I)]


def rule_sentiment(title: str, tags: list[str]) -> float:
    s = len(re.findall(POS_W, title, re.I)) - len(re.findall(NEG_W, title, re.I)) - 2 * len(NEG_TAGS & set(tags))
    return float(max(-2, min(2, s)))


def _rss(url: str) -> list[dict]:
    try:
        r = requests.get(url, headers=UA, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log.warning("news feed failed %s: %s", url[:80], e)
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        src = (it.findtext("source") or "").strip()
        if not src and " - " in title:  # Google News: "headline - Publisher"
            title, src = title.rsplit(" - ", 1)
        elif src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        pub = it.findtext("pubDate")
        try:
            pub = parsedate_to_datetime(pub).isoformat(timespec="seconds") if pub else None
        except (TypeError, ValueError):
            pub = None
        out.append({"title": title.strip(), "source": src, "link": it.findtext("link"), "published": pub})
        if len(out) >= MAX_PER_FEED:
            break
    return out


def _gnews(q: str, kr: bool, days: int = 2) -> list[dict]:
    qs = urllib.parse.quote(f"{q} when:{days}d")
    loc = "hl=ko&gl=KR&ceid=KR:ko" if kr else "hl=en-US&gl=US&ceid=US:en"
    return _rss(f"https://news.google.com/rss/search?q={qs}&{loc}")


def _yahoo(code: str) -> list[dict]:
    return _rss(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={urllib.parse.quote(code)}&region=US&lang=en-US")


def _dart(day: str, codes: set[str]) -> list[dict]:
    """Today's KRX disclosures for the given codes (needs DART_API_KEY)."""
    key = os.environ.get("DART_API_KEY")
    if not key:
        return []
    d = day.replace("-", "")
    out, page = [], 1
    while page <= 30:
        try:
            r = requests.get("https://opendart.fss.or.kr/api/list.json", timeout=20, params={
                "crtfc_key": key, "bgn_de": d, "end_de": d, "page_no": page, "page_count": 100})
            j = r.json()
        except Exception as e:
            log.warning("dart failed: %s", e)
            break
        if j.get("status") != "000":
            if j.get("status") != "013":  # 013 = no data
                log.warning("dart status %s %s", j.get("status"), j.get("message"))
            break
        for x in j.get("list", []):
            if x.get("stock_code") in codes:
                out.append({"code": x["stock_code"], "title": x["report_nm"].strip(), "source": "DART",
                            "link": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={x['rcept_no']}",
                            "published": f"{x['rcept_dt'][:4]}-{x['rcept_dt'][4:6]}-{x['rcept_dt'][6:]}"})
        if page >= int(j.get("total_page", 1)):
            break
        page += 1
        time.sleep(0.2)
    return out


def _insert(db, code: str, kind: str, items: list[dict], seen: str) -> int:
    n = 0
    for it in items:
        tags = rule_tags(it["title"])
        cur = db.conn.execute(
            "INSERT OR IGNORE INTO news(code,kind,published,seen,title,source,link,tags,sentiment,scorer) "
            "VALUES(?,?,?,?,?,?,?,?,NULL,NULL)",
            (code, kind, it.get("published"), seen, it["title"], it.get("source"), it.get("link"), json.dumps(tags)))
        n += cur.rowcount
    db.conn.commit()
    return n


SCORE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "sentiment": {"type": "integer", "minimum": -2, "maximum": 2},
                       "tags": {"type": "array", "items": {"type": "string", "enum": sorted(TAGS)}}},
        "required": ["id", "sentiment", "tags"], "additionalProperties": False}}},
    "required": ["items"],
    "additionalProperties": False,
}


def score(app, limit: int = 150) -> int:
    """Label unscored headlines: Claude in one batch when available, else keyword rules."""
    rows = app.db.query("SELECT id, code, title, tags FROM news WHERE sentiment IS NULL ORDER BY id DESC LIMIT ?", (limit,))
    if not rows:
        return 0
    labels = {}
    if app.claude_ready:
        from . import analyst as AI
        names = app.db.query("SELECT code, name FROM tickers WHERE code IN (%s)" % ",".join("?" * len({r["code"] for r in rows})),
                             tuple({r["code"] for r in rows}))
        nm = {r["code"]: r["name"] for r in names}
        lines = "\n".join(f'{r["id"]}\t{nm.get(r["code"], r["code"]) or "시장"}\t{r["title"]}' for r in rows)
        try:
            text = AI._call(app.cfg.raw, "당신은 주식 뉴스 분류기입니다. 각 헤드라인이 해당 종목(또는 시장 전체) 주가에 "
                            "단기적으로 어떤 영향을 줄지만 판단합니다.",
                            f"""아래 헤드라인마다 sentiment(-2 아주 나쁨 … 0 중립·무관 … +2 아주 좋음)와 해당하는 tags를 고르세요.
제목만 보고 판단하고, 확실하지 않으면 0. 형식: id<TAB>종목<TAB>제목

{lines}""", "low", schema=SCORE_SCHEMA)
            labels = {x["id"]: x for x in json.loads(text)["items"]}
        except Exception as e:
            log.warning("news scoring by Claude failed, using rules: %s", e)
    for r in rows:
        tags = json.loads(r["tags"] or "[]")
        x = labels.get(r["id"])
        if x:
            tags = sorted(set(tags) | set(x["tags"]))
            s, who = float(x["sentiment"]), "claude"
        else:
            s, who = rule_sentiment(r["title"], tags), "rule"
        app.db.conn.execute("UPDATE news SET sentiment=?, tags=?, scorer=? WHERE id=?", (s, json.dumps(tags), who, r["id"]))
    app.db.conn.commit()
    return len(rows)


def collect(app, codes: list[str], day: str) -> dict:
    """Fetch market + per-stock news (and KR disclosures), store new items, score them."""
    ensure(app.db)
    kr = not app.cfg.is_us
    seen = app.now().isoformat(timespec="seconds")
    n_mkt = sum(_insert(app.db, "", "news", _gnews(q, kr, 1), seen) for q in MARKET_Q["kr" if kr else "us"])
    codes = list(dict.fromkeys(c for c in codes if c))[:MAX_CODES]
    names = {r["code"]: r["name"] for r in app.db.query(
        "SELECT code, name FROM tickers WHERE code IN (%s)" % ",".join("?" * len(codes)), tuple(codes))} if codes else {}
    n_stock = 0
    for c in codes:
        items = _gnews(f'"{names.get(c) or c}"', True, 3) if kr else _yahoo(c)
        n_stock += _insert(app.db, c, "news", items, seen)
        time.sleep(0.3)
    n_disc = 0
    if kr:
        for it in _dart(day, set(codes)):
            n_disc += _insert(app.db, it["code"], "disclosure", [it], seen)
    scored = score(app)
    log.info("news: market +%s, stocks +%s (%s codes), disclosures +%s, scored %s", n_mkt, n_stock, len(codes), n_disc, scored)
    mood = market_mood(app.db, seen)
    hist = json.loads(app.db.get("news_mood") or "{}")
    hist[day] = mood
    app.db.set("news_mood", json.dumps(dict(sorted(hist.items())[-400:]), ensure_ascii=False))
    return {"market": n_mkt, "stocks": n_stock, "disclosures": n_disc, "scored": scored, "mood": mood}


def _since(asof: str, days: int) -> str:
    return (datetime.fromisoformat(asof) - timedelta(days=days)).isoformat(timespec="seconds")


def snapshot(db, code: str, asof: str, days: int = 3) -> dict:
    """What was known about ``code`` by ``asof`` (seen time): count, mean sentiment, tags, label."""
    try:
        rows = db.query("SELECT title, sentiment, tags, kind FROM news WHERE code=? AND seen<=? AND seen>=? ORDER BY id DESC",
                        (code, asof, _since(asof, days)))
    except Exception:
        return {"n": 0, "label": "unknown"}
    scored = [r["sentiment"] for r in rows if r["sentiment"] is not None]
    tags = sorted({t for r in rows for t in json.loads(r["tags"] or "[]")})
    sent = round(sum(scored) / len(scored), 2) if scored else None
    if NEG_TAGS & set(tags):
        label = "risk"
    elif not rows:
        label = "none"
    elif sent is not None and sent >= 0.5:
        label = "good"
    elif sent is not None and sent <= -0.5:
        label = "bad"
    else:
        label = "mixed"
    return {"n": len(rows), "sent": sent, "tags": tags, "label": label,
            "top": [r["title"] for r in rows[:3]]}


def market_mood(db, asof: str, days: int = 1) -> dict:
    s = snapshot(db, "", asof, days)
    return {"n": s["n"], "sent": s.get("sent"), "label": s["label"], "top": s.get("top", [])}


def recent(db, code: str, limit: int = 12) -> list[dict]:
    try:
        return db.query("SELECT * FROM news WHERE code=? ORDER BY COALESCE(published, seen) DESC LIMIT ?", (code, limit))
    except Exception:
        return []


LABEL_KO = {"good": "좋은 뉴스", "bad": "나쁜 뉴스", "mixed": "뉴스 섞임", "none": "뉴스 없음", "risk": "위험 공시·뉴스",
            "unknown": "확인 안 함"}
TAG_KO = {"capital_raise": "유상증자·CB", "capital_cut": "감자", "earnings": "실적", "contract": "계약·수주", "legal": "소송·수사",
          "rating": "목표가·의견", "buyback": "자사주", "mna": "인수합병", "delisting": "거래정지·관리", "macro": "거시경제"}

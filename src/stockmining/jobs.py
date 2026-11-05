"""Scheduled jobs: init, evening (장 마감 후), morning (장 전), weekly (주말 학습·연구), approvals."""
from __future__ import annotations

import json
import logging
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from . import docfmt as DF
from . import labels as LB
from . import analyst as AI
from . import backtest as BT
from . import data as D
from . import earnings as ER
from . import edgemap as EM
from . import engine as E
from . import journal as J
from . import learner as L
from . import regime as RG
from . import rules as R
from .config import ROOT, Config
from .db import DB
from . import notify

log = logging.getLogger(__name__)


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.path("db_path"))
        self.tg = notify.make(cfg.raw, self.db)  # messenger: Slack (default) / Telegram / console
        self.tz = ZoneInfo(cfg["timezone"])

    # ------------------------------------------------------------------ helpers
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> str:
        return self.now().strftime("%Y-%m-%d")

    def is_trading_day(self, day: str) -> bool:
        try:
            import exchange_calendars as xc
            return bool(xc.get_calendar(self.cfg["calendar"]).is_session(day))
        except Exception as e:  # calendar package outdated/unavailable → weekday fallback
            log.warning("calendar check failed (%s); using weekday", e)
            return pd.Timestamp(day).weekday() < 5

    # ------------------------------------------------------------------ learning log
    def open_insights(self, limit: int = 20) -> list[dict]:
        return self.db.query("SELECT id, ts, source, kind, text FROM insights WHERE status='open' "
                             "ORDER BY id DESC LIMIT ?", (limit,))

    def save_learning(self, source: str, out: dict) -> list[str]:
        """Store Claude's new insights and status updates; returns short lines for the report."""
        now = self.now().isoformat(timespec="seconds")
        known = {r["text"] for r in self.db.query("SELECT text FROM insights")}
        lines = []
        for i in out.get("new_insights", [])[:5]:
            if i["text"].strip() and i["text"] not in known:
                self.db.execute("INSERT INTO insights(ts,source,kind,text,status) VALUES(?,?,?,?, 'open')",
                                (now, source, i["kind"], i["text"].strip()))
                lines.append(f"📝 {i['kind']}: {i['text'].strip()}")
        open_ids = {r["id"] for r in self.open_insights(200)}
        for u in out.get("insight_updates", []):
            if u["id"] in open_ids and u["status"] != "open":
                self.db.execute("UPDATE insights SET status=?, note=? WHERE id=?", (u["status"], u["note"], u["id"]))
                lines.append(f"{'✅' if u['status'] == 'confirmed' else '❌'} 가설 #{u['id']} {u['status']}: {u['note']}")
        return lines

    def current_regime(self) -> str:
        reg = RG.regime_series(self.db.load_index(self.cfg["regime_index"]))
        return str(reg.iloc[-1]) if len(reg) else "neutral"

    def fmt(self, price) -> str:
        """Price for messages: USD with cents, KRW without decimals."""
        if price is None or (isinstance(price, float) and pd.isna(price)):
            return "-"
        return f"${price:,.2f}" if self.cfg.is_us else f"{price:,.0f}"

    @property
    def principles_path(self) -> Path:
        return self.cfg.path("principles_path")

    def principles(self) -> str:
        p = self.principles_path
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / ("principles.md" if self.cfg.is_us else "principles_kr.md"), p)
        return p.read_text(encoding="utf-8")

    @property
    def claude_ready(self) -> bool:
        return AI.backend(self.cfg.raw) != "none"

    def _bt_args(self):
        return (self.cfg["costs"],)

    def full_context(self):
        """Backtest context: ALL listed common stocks (not just today's universe) with the liquidity
        filter applied point-in-time via ``min_value`` — avoids picking stocks by today's liquidity."""
        years = self.cfg["universe"]["history_years"]
        codes = [r["code"] for r in self.db.query("SELECT code FROM tickers")]
        return E.load_context(self.db, self.cfg.raw, lookback_days=365 * years + 400, codes=codes)

    @property
    def min_value(self) -> float:
        return float(self.cfg["universe"]["min_avg_value_20"])

    def evaluate(self, spec: dict, ctx=None) -> dict:
        panel, regs, _, _ = ctx or self.full_context()
        BT.prepare_panel(panel, [R.resolve(spec)])
        return BT.evaluate_spec(panel, spec, self.cfg["costs"], regs, self.cfg["account"]["max_positions"],
                                self.cfg["backtest"]["oos_fraction"], self.min_value)

    # ------------------------------------------------------------------ init / seed
    def seed_strategies(self) -> list[str]:
        now = self.now().isoformat(timespec="seconds")
        existing = {s["name"]: s for s in self.db.strategies(("active", "muted", "retired"))}
        names = []
        files = sorted(self.cfg.path("strategies_dir").glob("*.yaml"))
        if not files:  # a wrong path must never retire every strategy
            raise FileNotFoundError(f"no strategy YAML in {self.cfg.path('strategies_dir')} (STOCKMINING_HOME?)")
        for f in files:
            spec = yaml.safe_load(f.read_text(encoding="utf-8"))
            R.validate(spec)
            cur = existing.get(spec["name"])
            if cur and cur["source"] != "builtin":
                continue
            if cur:  # keep tuned params and learned status
                spec["params"] = {**spec.get("params", {}), **cur["spec"].get("params", {})}
            self.db.save_strategy(spec, "builtin", cur["status"] if cur else "active", now)
            names.append(spec["name"])
        for name, cur in existing.items():  # built-in whose YAML was removed → retire (history is kept)
            if cur["source"] == "builtin" and name not in names and cur["status"] != "retired":
                self.db.execute("UPDATE strategies SET status='retired', updated=? WHERE name=?", (now, name))
                log.info("retired removed built-in strategy %s", name)
        return names

    def init(self, skip_backfill: bool = False, limit: int | None = None) -> None:
        self.principles()
        log.info("seeded strategies: %s", self.seed_strategies())
        D.update_indices(self.db, self.cfg.raw)
        codes = D.refresh_listing(self.db, self.cfg.raw)
        if not skip_backfill:
            n = D.update_prices(self.db, self.cfg.raw, codes[:limit] if limit else codes)
            log.info("downloaded %d rows", n)
        D.refresh_universe(self.db, self.cfg.raw)
        ctx = self.full_context()
        self.backtest_all(ctx)
        self.replay(ctx)

    def backtest_all(self, ctx=None) -> dict:
        ctx = ctx or self.full_context()
        out = {}
        now = self.now().isoformat(timespec="seconds")
        for st in self.db.strategies(("active", "muted")):
            try:
                res = self.evaluate(st["spec"], ctx)
            except Exception as e:
                log.error("backtest %s failed: %s", st["name"], e)
                continue
            self.db.execute("UPDATE strategies SET backtest=?, updated=? WHERE name=?",
                            (json.dumps(res), now, st["name"]))
            out[st["name"]] = res
            f = res["full"]
            log.info("%-22s trades=%s win=%s%% exp=%s%% PF=%s CAGR=%s%% MDD=%s%% | OOS PF=%s",
                     st["name"], f.get("trades"), f.get("win_rate"), f.get("expectancy_pct"),
                     f.get("profit_factor"), f.get("cagr_pct"), f.get("mdd_pct"), res["oos"].get("profit_factor"))
        return out

    def replay(self, ctx=None) -> dict | None:
        from . import replay as RP
        try:
            return RP.run(self, ctx)
        except Exception as e:
            log.error("replay failed: %s", e, exc_info=True)
            return None

    # ------------------------------------------------------------------ evening
    def evening(self, day: str | None = None, force: bool = False) -> None:
        day = day or self.today()
        if not force and not self.is_trading_day(day):
            log.info("%s is not a trading day", day)
            return
        upd = D.daily_update(self.db, self.cfg.raw, day)
        if not upd["ok"]:
            self.tg.send(f"[저녁 배치] 데이터 갱신 실패: {upd['reason']}")
            return
        self.run_engine(day)
        self.live()

    def live(self, ctx=None, notify: bool = True) -> dict | None:
        """Forward mock investing (10M per method from its start day, see live.py)."""
        from . import live as LV
        try:
            st = LV.update(self, ctx)
        except Exception as e:
            log.error("live mock investing failed: %s", e, exc_info=True)
            return None
        if notify and st and st["last"] != st["start"]:
            self.tg.send(LV.slack_lines(st))
        return st

    def run_engine(self, day: str) -> dict:
        cfg = self.cfg.raw
        panel, regs, idx, info = E.load_context(self.db, cfg)
        all_strats = {s["name"]: s for s in self.db.strategies(("active", "muted", "retired"))}
        paper_stats = E.update_paper_trades(self.db, panel, all_strats, cfg["costs"])

        live = [s for s in all_strats.values() if s["status"] in ("active", "muted")]
        signals = E.generate_signals(self.db, panel, regs, live, day)
        E.open_paper_trades(self.db, signals, cfg["learner"]["paper_top_k"])

        market = {name: RG.describe(df) for name, df in idx.items()}
        regime = market.get(cfg["regime_index"], {}).get("regime", "neutral")
        stats = L.strategy_stats(self.db, live, regime, cfg)
        changes = L.update_statuses(self.db, stats, cfg)
        if changes:  # re-read statuses so weights reflect them
            live = self.db.strategies(("active", "muted"))
            stats = L.strategy_stats(self.db, live, regime, cfg)
        weights = L.weights_from_stats(stats, cfg["selection"], cfg["strategy_include"])

        positions, trips = J.positions_and_roundtrips(self.db)
        alerts = J.position_alerts(positions, panel, self.fmt)
        bdays = cfg["account"].get("earnings_blackout_days", 5)
        blackout = (lambda code: ER.in_blackout(self.db, code, day, bdays)) if self.cfg.is_us else None
        recs = E.recommend(self.db, signals, weights, cfg, set(positions), day, blackout)
        skipped = recs.attrs.get("skipped", []) if not recs.empty else []
        if self.cfg.is_us:  # holdings: warn before earnings so the exit plan is set in advance
            for code in positions:
                hit, nxt = ER.in_blackout(self.db, code, day, 3)
                if hit:
                    alerts.append(f"📅 {code} 실적 발표 {nxt} — 발표 전 청산 또는 절반 축소 결정 (원칙 3)")
        disc = J.discipline(self.db, trips, cfg)
        disc["open_position_issues"] = J.open_position_issues(positions, cfg)
        if self.cfg.is_us:
            for code, p in positions.items():
                p["next_earnings"] = ER.next_earnings(self.db, code, day)

        names = {c: i.get("name") for c, i in info.items()}
        rec_list = [] if recs.empty else [
            {"code": r.code, "name": names.get(r.code), "strategy": r.strategy, "entry_ref_close": r.close,
             "stop": round(r.stop, 2), "target": None if pd.isna(r.target) else round(r.target, 2),
             "shares": r.shares, "risk_amount": round((r.close - r.stop) * r.shares, 2),
             "currency": cfg["currency"], "next_earnings": getattr(r, "next_earnings", None)}
            for r in recs.itertuples()]
        ctx = {
            "date": day, "market": market, "learner_changes": changes,
            "strategies": stats, "weights": {k: round(v, 3) for k, v in weights.items()},
            "signals_count": signals.groupby("strategy").size().to_dict() if not signals.empty else {},
            "recommendations": rec_list,
            "skipped_for_earnings": [{"code": c, "next_earnings": d} for c, d in skipped],
            "positions": list(positions.values()), "alerts": alerts,
            "discipline": disc, "recent_roundtrips": trips[-10:],
            "paper_update": paper_stats, "account": cfg["account"],
            "profile": cfg["profile"],
            "edge_summary": self.db.get("edge_summary") or "아직 없음(주간 배치 후 생성)",
            "open_insights": self.open_insights(),
        }
        self.db.set("last_evening_ctx", json.dumps(ctx, ensure_ascii=False, default=str))

        card = self._evening_card(ctx)
        comment, extra, doc, learned = "", "", None, []
        if self.claude_ready:
            try:
                out = AI.evening_report(self.cfg.raw, self.principles(), ctx)
                doc = out["comment"]
                comment = DF.to_slack(doc)
                extra += self._handle_ai_proposals(out.get("proposals", []))
                learned = self.save_learning("evening", out)
                if learned:
                    extra += f"\n\n🧠 시스템이 새로 배운 것 {len(learned)}개 · 대시보드 '학습 일지'에서 보기"
            except Exception as e:
                log.error("claude evening failed: %s\n%s", e, traceback.format_exc())
                comment = f"(Claude 코멘트 실패: {e})"
        body = card + (f"\n\n💬 {comment}" if comment else "") + extra
        self.db.add_report("evening", body, self.now().isoformat(timespec="seconds"),
                           {"doc": doc, "learned": [l.lstrip("📝 ") for l in learned]} if doc else None)
        self.tg.send(body)
        return ctx

    def _evening_card(self, ctx: dict) -> str:
        """Fixed-format summary the user reads first: what to buy, at what price, how much, worst case."""
        cap = ctx["account"].get("capital") or 0
        d = pd.Timestamp(ctx["date"])
        recs = ctx["recommendations"] or []
        lines = [f"📊 *저녁 리포트* · {d.month}/{d.day} 장 마감", ""]
        if recs:
            lines.append(f"✅ *내일 살 후보 {len(recs)}개*")
        else:
            lines.append("😴 *내일 살 종목 없음* — 쉬어 가는 날이에요")
        for r in recs:
            price, stop, sh = r["entry_ref_close"], r["stop"], r["shares"] or 0
            cost = price * sh
            drop = (stop / price - 1) * 100 if price and stop else None
            lines += [
                "",
                f"*{r['code']}* {r['name'] or ''} · {LB.strat(r['strategy'])}",
                f"• 살 가격  {self.fmt(price)} 근처 (지정가, 크게 갭상승하면 쫓아가지 않기)",
                f"• 손절가  {self.fmt(stop)}" + (f"  ({abs(drop):.0f}% 빠지면 판다)" if drop is not None else ""),
                "• 목표가  " + (self.fmt(r["target"]) if r["target"] else "없음 — 오르는 동안 들고 가며 손절가를 올림"),
                f"• 수량  {round(sh, 2):g}주 ≈ {self.fmt(cost)}" + (f" (내 돈의 {cost / cap:.0%})" if cap else ""),
                f"• 틀리면  최대 {self.fmt(r['risk_amount'])} 손실" + (f" (내 돈의 {r['risk_amount'] / cap:.0%})" if cap else ""),
            ]
            if r.get("next_earnings"):
                lines.append(f"• 다음 실적 발표  {r['next_earnings']}")
        for s_ in ctx.get("skipped_for_earnings", []):
            lines.append(f"\n🚫 {s_['code']} 은 뺐어요 — 실적 발표({s_['next_earnings']})가 코앞이라 위험")
        if ctx["alerts"]:
            lines += ["", "⚠️ *보유 종목 확인*"] + [f"• {a.lstrip('⚠️🎯📅 ')}" for a in ctx["alerts"]]
        elif ctx["positions"]:
            lines += ["", f"📦 보유 {len(ctx['positions'])}종목 — 손절·목표 도달 없음"]
        ri = ctx["market"].get(self.cfg["regime_index"], {})
        mood = f"\n🌡️ 시장: {LB.regime(ri.get('regime'))}"
        if ri.get("chg_20d_pct") is not None:
            idx_name = "S&P500" if self.cfg.is_us else self.cfg["regime_index"]
            mood += f" ({idx_name} 최근 한 달 {ri['chg_20d_pct']:+.1f}%)"
        lines.append(mood)
        return "\n".join(lines)

    # ------------------------------------------------------------------ proposals
    def _store_proposal(self, kind, title, rationale, payload, bt=None, passed=None) -> int:
        cur = self.db.execute(
            """INSERT INTO proposals(created,kind,title,rationale,payload,backtest,passed,status)
               VALUES(?,?,?,?,?,?,?,'pending')""",
            (self.now().isoformat(timespec="seconds"), kind, title, rationale, payload,
             json.dumps(bt) if bt else None, passed))
        return cur.lastrowid

    def test_rule(self, spec: dict, ctx=None) -> tuple[dict | None, bool, list[str]]:
        try:
            R.validate(spec)
        except R.SpecError as e:
            return None, False, [f"명세 오류: {e}"]
        if spec["name"] in {s["name"] for s in self.db.strategies(("active", "muted", "retired"))}:
            spec["name"] = (spec["name"] + "_v2")[:41]
        res = self.evaluate(spec, ctx)
        ok, why = BT.passes_gate(res, self.cfg["backtest"]["gate"])
        return res, ok, why

    def _handle_ai_proposals(self, proposals: list[dict], ctx=None) -> str:
        if not proposals:
            return ""
        lines = ["\n\n🧪 제안"]
        for p in proposals:
            if p["kind"] == "principle" and p["principle_text"].strip():
                pid = self._store_proposal("principle", p["title"], p["rationale"], p["principle_text"].strip())
                lines.append(f"#{pid} [원칙] {p['title']} — {p['principle_text'].strip()}\n  승인: /approve {pid}  거절: /reject {pid}")
            elif p["kind"] == "rule" and p["rule_spec_json"].strip():
                lines.append(self._rule_proposal(p["title"], p["rationale"], p["rule_spec_json"], ctx))
        return "\n".join(lines)

    def _rule_proposal(self, title, rationale, spec_json, ctx=None) -> str:
        try:
            spec = json.loads(spec_json)
        except json.JSONDecodeError:
            return f"[규칙] {title}: JSON 파싱 실패로 폐기"
        res, ok, why = self.test_rule(spec, ctx)
        pid = self._store_proposal("rule", title, rationale, json.dumps(spec, ensure_ascii=False), res, int(ok))
        f = (res or {}).get("full", {})
        o = (res or {}).get("oos", {})
        summ = (f"거래 {f.get('trades')} 승률 {f.get('win_rate')}% 기대값 {f.get('expectancy_pct')}% "
                f"PF {f.get('profit_factor')} MDD {f.get('mdd_pct')}% | OOS PF {o.get('profit_factor')}") if res else ""
        if ok:
            return f"#{pid} [규칙 ✅ 게이트 통과] {title}\n  {summ}\n  {rationale[:300]}\n  승인: /approve {pid}  거절: /reject {pid}"
        self.db.execute("UPDATE proposals SET status='rejected', decided=? WHERE id=?",
                        (self.now().isoformat(timespec="seconds"), pid))
        return f"#{pid} [규칙 ❌ 자동 기각] {title}: {'; '.join(why)}"

    def decide(self, pid: int, approve: bool) -> str:
        rows = self.db.query("SELECT * FROM proposals WHERE id=?", (pid,))
        if not rows:
            return f"제안 #{pid} 없음"
        p = rows[0]
        if p["status"] != "pending":
            return f"제안 #{pid}는 이미 {p['status']} 상태"
        now = self.now().isoformat(timespec="seconds")
        if not approve:
            self.db.execute("UPDATE proposals SET status='rejected', decided=? WHERE id=?", (now, pid))
            return f"제안 #{pid} 거절"
        if p["kind"] == "rule" and not p["passed"]:
            return f"제안 #{pid}는 백테스트 게이트를 통과하지 못해 승인할 수 없습니다"
        if p["kind"] == "rule":
            spec = json.loads(p["payload"])
            self.db.save_strategy(spec, "claude", "active", now, json.loads(p["backtest"]) if p["backtest"] else None)
            entry = f"- {now[:10]} 규칙 추가 `{spec['name']}`: {p['title']} (제안 #{pid})"
        elif p["kind"] == "refine":
            payload = json.loads(p["payload"])
            st = {s["name"]: s for s in self.db.strategies(("active", "muted"))}.get(payload["strategy"])
            if not st:
                return "대상 전략 없음"
            self.db.save_strategy(payload["spec"], st["source"], st["status"], now,
                                  json.loads(p["backtest"]) if p["backtest"] else None)
            entry = f"- {now[:10]} `{payload['strategy']}` 진입 조건 추가 `{payload['condition']}` (엣지 맵, 제안 #{pid})"
        elif p["kind"] == "params":
            payload = json.loads(p["payload"])
            st = {s["name"]: s for s in self.db.strategies(("active", "muted"))}.get(payload["strategy"])
            if not st:
                return "대상 전략 없음"
            spec = dict(st["spec"], params={**st["spec"]["params"], **payload["params"]})
            self.db.save_strategy(spec, "builtin" if st["source"] == "builtin" else st["source"], st["status"], now,
                                  json.loads(p["backtest"]) if p["backtest"] else None)
            entry = f"- {now[:10]} `{payload['strategy']}` 파라미터 변경 {payload['params']} (제안 #{pid})"
        else:
            entry = f"- {now[:10]} {p['payload']} (제안 #{pid})"
        self.principles()
        with self.principles_path.open("a", encoding="utf-8") as fh:
            fh.write(entry + "\n")
        self.db.execute("UPDATE proposals SET status='applied', decided=? WHERE id=?", (now, pid))
        return f"제안 #{pid} 반영 완료\n{entry}"

    # ------------------------------------------------------------------ morning
    def morning(self, day: str | None = None, force: bool = False) -> None:
        day = day or self.today()
        if not force and not self.is_trading_day(day):
            return
        last = self.db.query("SELECT MAX(date) d FROM recommendations")[0]["d"]
        recs = self.db.query("SELECT * FROM recommendations WHERE date=?", (last,)) if last else []
        info = self.db.ticker_info()
        positions, _ = J.positions_and_roundtrips(self.db)
        ctx = {
            "date": day,
            "candidates": [{"code": r["code"], "name": info.get(r["code"], {}).get("name"), "strategy": r["strategy"],
                            "entry_ref": r["entry_ref"], "stop": r["stop"], "target": r["target"],
                            "shares": r["shares"]} for r in recs],
            "holdings": [dict(p, name=info.get(c, {}).get("name")) for c, p in positions.items()],
        }
        if not self.claude_ready:
            self.tg.send(f"☀️ 아침 {day}: 후보 {len(recs)}개 (Claude 키 없음 → 브리핑 생략)")
            return
        try:
            out = AI.morning_briefing(self.cfg.raw, self.principles(), ctx)
        except Exception as e:
            log.error("morning failed: %s", e)
            self.tg.send(f"☀️ 아침 브리핑 실패: {e}")
            return
        for code, why in out["avoid"].items():
            self.db.execute("UPDATE recommendations SET flagged=? WHERE date=? AND code=?", (why, last, code))
        body = out["text"]
        if out["avoid"]:
            body += "\n\n⛔ 오늘 보류: " + ", ".join(f"{info.get(c, {}).get('name', c)}({c})" for c in out["avoid"])
        self.db.add_report("morning", body, self.now().isoformat(timespec="seconds"), {"doc": out.get("doc")})
        self.tg.send(f"☀️ 아침 브리핑 {day}\n\n{body}")

    # ------------------------------------------------------------------ weekly
    def weekly(self) -> None:
        cfg = self.cfg.raw
        lines = [f"🧠 주간 학습 {self.today()}"]
        ctx = self.full_context()
        panel, regs, _, _ = ctx
        now = self.now().isoformat(timespec="seconds")

        # 1) walk-forward parameter tuning
        for st in self.db.strategies(("active", "muted")):
            try:
                BT.prepare_panel(panel, [R.resolve(st["spec"])])
                t = BT.walk_forward_tune(panel, st["spec"], cfg["costs"], regs, cfg["account"]["max_positions"],
                                         cfg["backtest"]["oos_fraction"], min_value=self.min_value)
            except Exception as e:
                log.error("tune %s failed: %s", st["name"], e)
                continue
            if not t or not t["improved"]:
                continue
            o_new, o_old = t["result"]["oos"], t["baseline"]["oos"]
            msg = (f"{st['name']} 파라미터 {t['params']}: OOS 기대값 {o_old.get('expectancy_pct')}% → "
                   f"{o_new.get('expectancy_pct')}% (PF {o_new.get('profit_factor')})")
            payload = json.dumps({"strategy": st["name"], "params": t["params"]})
            pid = self._store_proposal("params", f"{st['name']} 파라미터 조정", msg, payload, t["result"], 1)
            if cfg["learner"]["auto_adopt_params"]:
                lines.append("⚙️ 자동 반영 " + msg + "\n  " + self.decide(pid, True).splitlines()[0])
            else:
                lines.append(f"#{pid} ⚙️ {msg}  /approve {pid}")

        # 2) refresh backtests (priors for the learner) + time-machine replay for the dashboard
        self.backtest_all(ctx)
        self.replay(ctx)

        # 3) edge map: where does each strategy win/lose? strong findings → filter → gate → proposal
        edge = {}
        try:
            edge = EM.build(self, ctx)
            summary = EM.summarize(edge)
            self.db.set("edge_map", json.dumps(edge, ensure_ascii=False, default=str))
            self.db.set("edge_summary", summary)
            lines.append("\n🔍 엣지 맵 (돈이 벌리고 잃는 조건)\n" + summary)
            pending = {json.loads(r["payload"]).get("condition") + "|" + json.loads(r["payload"]).get("strategy")
                       for r in self.db.query("SELECT payload FROM proposals WHERE kind='refine'")}
            for r in EM.refinements(self, edge, ctx):
                key = f"{r['condition']}|{r['strategy']}"
                if not r["passed"] or key in pending:
                    continue
                b, n = r["baseline"], r["result"]
                msg = (f"{r['strategy']}에 `{r['condition']}` 추가: 구간 내 기대값 {b['is'].get('expectancy_pct')}%→"
                       f"{n['is'].get('expectancy_pct')}%, 표본 외 PF {b['oos'].get('profit_factor')}→{n['oos'].get('profit_factor')}")
                payload = json.dumps({"strategy": r["strategy"], "condition": r["condition"], "spec": r["spec"]},
                                     ensure_ascii=False)
                pid = self._store_proposal("refine", f"{r['strategy']} 필터 추가", msg, payload, n, 1)
                lines.append(f"#{pid} 🔧 {msg}\n  승인: /approve {pid}")
        except Exception as e:
            log.error("edge map failed: %s\n%s", e, traceback.format_exc())
            lines.append(f"엣지 맵 실패: {e}")

        # 4) Claude web research (+ open hypotheses, edge map) → new rule candidates → backtest gate
        if self.claude_ready:
            week = int(self.now().strftime("%V"))
            topics = AI.RESEARCH_TOPICS_US if self.cfg.is_us else AI.RESEARCH_TOPICS
            topic = topics[week % len(topics)]
            strategies = self.db.strategies(("active", "muted"))
            regime = self.current_regime()
            status = {"strategy_names": [s["name"] for s in strategies],
                      "strategies": L.strategy_stats(self.db, strategies, regime, cfg),
                      "regime": regime,
                      "recent_proposals": self.db.query(
                          "SELECT id,kind,title,status,passed FROM proposals ORDER BY id DESC LIMIT 15"),
                      "open_insights": self.open_insights(),
                      "edge_map": {k: v["findings"][:4] for k, v in edge.items()}}
            try:
                notes, out = AI.research_rules(cfg, self.principles(), status, topic)
                self.db.add_report("weekly_research", f"주제: {topic}\n\n{notes}", now)
                lines.append(f"\n📚 리서치 주제: {topic}")
                lines += self.save_learning("weekly", out)
                for c in out["candidates"]:
                    lines.append(self._rule_proposal(c["title"], f"{c['rationale']} (출처: {c['source']})",
                                                     c["rule_spec_json"], ctx))
            except Exception as e:
                log.error("research failed: %s\n%s", e, traceback.format_exc())
                lines.append(f"리서치 실패: {e}")

        # 5) leaderboard
        regime = self.current_regime()
        lines.append(f"\n현재 국면: {regime}")
        for s in sorted(L.strategy_stats(self.db, self.db.strategies(("active", "muted")), regime, cfg),
                        key=lambda s: -s["posterior_expectancy_pct"]):
            lines.append(f"- {s['name']} [{s['status']}] 사후기대값 {s['posterior_expectancy_pct']}% "
                         f"(페이퍼 {s['paper_trades']}건 {s['paper_expectancy_pct']}%)")
        body = "\n".join(lines)
        self.db.add_report("weekly", body, now)
        self.tg.send(body)

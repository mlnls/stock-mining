"""Command line entry point: ``stockmining <command>``."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import load_config


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="stockmining")
    p.add_argument("--config", help="config.yaml path")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="DB 생성, 전략 시드, 과거 데이터 백필, 기준 백테스트")
    s.add_argument("--skip-backfill", action="store_true")
    s.add_argument("--limit", type=int, help="백필 종목 수 제한(테스트용)")
    s = sub.add_parser("evening", help="장 마감 후 배치")
    s.add_argument("--date")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("morning", help="장 전 브리핑")
    s.add_argument("--date")
    s.add_argument("--force", action="store_true")
    sub.add_parser("weekly", help="주간 학습: 파라미터 튜닝 + Claude 리서치 + 백테스트 갱신")
    sub.add_parser("bot", help="텔레그램 봇 (상시 실행)")
    sub.add_parser("backtest", help="모든 전략 백테스트 + 타임머신 모의투자(대시보드 /sim)")
    sub.add_parser("replay", help="타임머신 모의투자만 다시 계산 (대시보드 /sim)")
    sub.add_parser("live", help="지금부터 모의투자: 시작(처음 한 번) 또는 밀린 날 처리")
    s = sub.add_parser("test-rule", help="JSON/YAML 규칙 파일 백테스트")
    s.add_argument("path")
    s = sub.add_parser("engine", help="데이터 갱신 없이 엔진만 실행 (최근 데이터 날짜 기준)")
    s.add_argument("--date")
    s = sub.add_parser("simulate", help="워크포워드 모의투자: 전략 선택 방식·분산 비교 + 1년 성과 확률(몬테카를로)")
    s.add_argument("--profiles", action="store_true", help="안정형/기본형/공격형 비교 + 원금 기준 예상 금액")
    s.add_argument("--start-amount", type=float, default=1_000_000, help="예상 금액 계산 원금 (기본 100만)")
    s = sub.add_parser("hold", help="내 자산(장기 보유) 등록: hold 코드 수량 평단 / hold rm 코드 / hold")
    s.add_argument("args", nargs="*")
    s = sub.add_parser("web", help="보기 전용 웹 대시보드 (WEB_PASSWORD 로 잠금)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s = sub.add_parser("notify", help="메신저로 메시지 보내기 (배치 실패 알림 등)")
    s.add_argument("text")
    s = sub.add_parser("cmd", help="텔레그램 명령을 로컬에서 실행 (예: cmd '/pos')")
    s.add_argument("text")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    for noisy in ("urllib3", "httpx", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if a.cmd == "web":
        from . import web
        web.serve(load_config(a.config), a.host, a.port)
        return

    from .jobs import App
    app = App(load_config(a.config))

    if a.cmd == "init":
        app.init(skip_backfill=a.skip_backfill, limit=a.limit)
    elif a.cmd == "evening":
        app.evening(a.date, a.force)
    elif a.cmd == "morning":
        app.morning(a.date, a.force)
    elif a.cmd == "weekly":
        app.weekly()
    elif a.cmd == "bot":
        from . import bot
        bot.run(app)
    elif a.cmd == "hold":
        from . import bot
        print(bot.handle(app, "/hold " + " ".join(a.args)))
    elif a.cmd == "backtest":
        app.seed_strategies()
        ctx = app.full_context()
        app.backtest_all(ctx)
        app.replay(ctx)
    elif a.cmd == "replay":
        app.replay()
    elif a.cmd == "live":
        app.live(notify=False)
    elif a.cmd == "test-rule":
        import yaml
        spec = yaml.safe_load(open(a.path, encoding="utf-8"))
        res, ok, why = app.test_rule(spec)
        print(json.dumps({"passed": ok, "why": why, "result": res}, ensure_ascii=False, indent=1, default=str))
    elif a.cmd == "engine":
        app.run_engine(a.date or app.db.last_price_date())
    elif a.cmd == "simulate":
        from . import simulate as SIM
        if a.profiles:
            for p in SIM.compare_profiles(app):
                m, o = p["metrics"], p["odds"]
                print(f"\n■ {p['label']}\n  거래 {m.get('trades')} 승률 {m.get('win_rate')}% 거래당 {m.get('expectancy_pct')}% "
                      f"CAGR {m.get('cagr_pct')}% MDD {m.get('mdd_pct')}% 최악의 달 {p['worst_month_pct']}% "
                      f"월승률 {o.get('pos_month_pct')}%")
                for label, hc in (("시뮬레이션 그대로", 0.0), ("우위 절반 할인(현실적)", 0.5)):
                    proj = SIM.project(p["monthly"], a.start_amount, haircut=hc)
                    for y, v in proj.items():
                        print(f"  [{label}] {y}년 후: 하위5% {v['p5']/1e4:,.0f} / 중앙 {v['median']/1e4:,.0f} / "
                              f"상위5% {v['p95']/1e4:,.0f}만 | 원금손실 {v['p_loss']:.0f}% 반토막 {v['p_half']:.0f}% "
                              f"낙폭30%↑ {v['p_dd30']:.0f}%")
            return
        for r in SIM.run(app):
            m, o = r.metrics, r.odds
            print(f"{r.name:14s} 동시{r.max_positions:>2}종목 | 거래 {m.get('trades', 0):4} 승률 {m.get('win_rate')}% "
                  f"기대값 {m.get('expectancy_pct')}% CAGR {m.get('cagr_pct')}% MDD {m.get('mdd_pct')}% | "
                  f"1년 수익확률 {o.get('p_profit_1y')}% >20% {o.get('p_gt_20pct')}% "
                  f"-10%↓ {o.get('p_loss_gt_10pct')}% MDD>20% {o.get('p_drawdown_gt_20pct')}% "
                  f"중앙값 {o.get('median_1y_pct')}% (5~95%: {o.get('p5_1y_pct')}~{o.get('p95_1y_pct')}%) "
                  f"월승률 {o.get('pos_month_pct')}%")
        for mp in (5, 10):
            print(f"무작위 선택 기준선(동시 {mp}종목):", SIM.luck_check(app, max_positions=mp))
    elif a.cmd == "notify":
        app.tg.send(a.text)
    elif a.cmd == "cmd":
        from . import bot
        print(bot.handle(app, a.text))


if __name__ == "__main__":
    main()

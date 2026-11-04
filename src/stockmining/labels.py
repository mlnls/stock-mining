"""Plain-Korean names for strategies and regimes, used in messages and the dashboard."""
from __future__ import annotations

STRATEGY_KO = {
    "bb_trend_pullback": "상승추세 눌림목",
    "donchian_turtle": "터틀 신고가 돌파",
    "earnings_gap_drift": "실적 갭상승 추종",
    "high52_momentum": "52주 신고가",
    "minervini_vcp": "미너비니 수축 돌파",
    "qullamaggie_breakout": "주도주 돌파",
    "rsi2_reversion": "단기 과매도 반등",
    "squeeze_breakout": "변동성 수축 돌파",
    "volatility_breakout": "변동성 돌파",
    "volume_thrust": "거래량 급증",
}
REGIME_KO = {"bull": "상승장", "neutral": "보통", "bear": "하락장"}


def strat(name: str | None) -> str:
    if not name:
        return "-"
    return STRATEGY_KO.get(name, name.replace("_", " "))


def regime(r: str | None) -> str:
    return REGIME_KO.get(r or "", r or "-")

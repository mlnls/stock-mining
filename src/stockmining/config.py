"""Configuration: YAML file + environment variables for secrets."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(os.environ.get("STOCKMINING_HOME", Path(__file__).resolve().parents[2]))

DEFAULTS: dict[str, Any] = {
    "market": "us",                 # us | kr  — per-market defaults below are layered under config.yaml
    "profile": "normal",            # normal | aggressive (see PROFILES)
    "selection": "learner",         # learner: weight by regime posterior expectancy · equal: equal weight
    "strategy_include": [],         # non-empty → only these strategies may produce recommendations
    "principles_path": "data/principles.md",
    "strategies_dir": "strategies",
    "account": {
        "risk_per_trade": 0.01,
        "max_positions": 5,
        "max_new_per_day": 2,
        "max_position_weight": 0.25,
        "max_stop_pct": 15,         # skip candidates whose stop is more than 15% below entry
        "fractional_shares": False,  # True → share counts with 4 decimals (US brokers' 소수점 거래)
        "earnings_blackout_days": 5, # US: skip candidates with earnings within N trading days
    },
    "universe": {"history_years": 4},
    "backtest": {
        "oos_fraction": 0.25,
        "gate": {
            "min_trades": 40,
            "min_profit_factor": 1.2,
            "min_expectancy_pct": 0.3,
            "min_oos_profit_factor": 1.05,
            "max_mdd_pct": 35,
        },
    },
    "learner": {
        "prior_trades": 30,
        "min_paper_trades_to_mute": 20,
        "paper_top_k": 3,
        "auto_adopt_params": True,
    },
    "claude": {
        # auto: ANTHROPIC_API_KEY set → API (billed) · else `claude` CLI found → Claude Code headless on your
        # Claude subscription (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`) · else Claude is skipped
        "backend": "auto",           # auto | api | cli | none
        "cli_model": "",             # optional --model for the CLI (e.g. "sonnet"); empty = plan default
        "model": "claude-opus-5-5",
        "effort_daily": "medium",
        "effort_research": "high",
        "web_search_max_uses": 8,
    },
    "notifier": "slack",            # slack | telegram | console (secrets come from env, see notify.py)
}

MARKET_DEFAULTS: dict[str, dict[str, Any]] = {
    # US: NYSE + NASDAQ via Yahoo (FinanceDataReader). Trading date follows New York time.
    "us": {
        "db_path": "data/stockmining_us.db",
        "timezone": "America/New_York",
        "calendar": "XNYS",
        "currency": "USD",
        "regime_index": "US500",          # S&P 500
        "info_indices": ["US500", "IXIC", "VIX"],
        "account": {"capital": 10_000},   # USD
        "universe": {
            "pool_per_exchange": 800,     # top-N by market cap from each of NASDAQ and NYSE (+ all S&P 500)
            "min_price": 5,
            "min_marcap": 0,
            "min_avg_value_20": 20_000_000,   # 20-day avg dollar volume ≥ $20M
            "exclude_name_patterns": ["ETF", "ETN", " Fund", "Warrant", "Rights", " Units", "Preferred",
                                      "Acquisition Corp"],
        },
        # Korean broker on US stocks: commission ~0.07–0.25% (0.1% assumed), SEC fee on sells ~0.003%.
        # FX spread is paid once when converting KRW→USD, not per trade, so it's not modelled here.
        "costs": {"commission": 0.001, "sell_tax": 0.00003, "slippage": 0.0005},
    },
    "kr": {
        "db_path": "data/stockmining.db",
        "timezone": "Asia/Seoul",
        "calendar": "XKRX",
        "currency": "KRW",
        "regime_index": "KS11",
        "info_indices": ["KS11", "KQ11"],
        "account": {"capital": 10_000_000},
        "universe": {
            "min_price": 0,
            "min_marcap": 100_000_000_000,
            "min_avg_value_20": 3_000_000_000,
            "exclude_name_patterns": ["스팩", "리츠", "ETN"],
        },
        "costs": {"commission": 0.00015, "sell_tax": 0.0020, "slippage": 0.001},
    },
}


# Risk profiles, layered between market defaults and config.yaml (config.yaml still wins).
PROFILES: dict[str, dict[str, Any]] = {
    "normal": {},
    # 공격형: concentrate in few high-expectancy trend/breakout strategies with bigger bets. No leverage.
    "aggressive": {
        "account": {
            "risk_per_trade": 0.02,
            "max_positions": 3,
            "max_new_per_day": 2,
            "max_position_weight": 0.40,
            "max_stop_pct": 20,
        },
        "selection": "equal",
        "strategy_include": ["qullamaggie_breakout", "minervini_vcp", "earnings_gap_drift", "donchian_turtle"],
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    raw: dict

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    @property
    def is_us(self) -> bool:
        return self.raw["market"] == "us"

    def path(self, key: str) -> Path:
        p = Path(self.raw[key])
        return p if p.is_absolute() else ROOT / p

    @property
    def anthropic_api_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY")


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("STOCKMINING_CONFIG", ROOT / "config.yaml"))
    data: dict = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    market = data.get("market", DEFAULTS["market"])
    if market not in MARKET_DEFAULTS:
        raise ValueError(f"market must be one of {list(MARKET_DEFAULTS)}")
    profile = data.get("profile", DEFAULTS["profile"])
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {list(PROFILES)}")
    return Config(_merge(_merge(_merge(DEFAULTS, MARKET_DEFAULTS[market]), PROFILES[profile]), data))

"""Settings (config.yaml) and secrets (.env).

Settings are plain knobs anyone may read. Secrets are API keys: they are read from
the environment once, kept in a Keys object whose repr never shows a value, and
never logged or printed. The defaults below are the single source of truth for what
a setting means; config.yaml overrides them.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    """config.yaml is present but does not make sense. The message says what is wrong."""


DEFAULTS: dict[str, Any] = {
    "refresh_seconds": 60,
    "top_strip": {
        "equities": ["SPY", "QQQ"],
        "fred": {"VIX": "VIXCLS", "10Y": "DGS10"},
    },
    "alpaca": {
        "feed": "iex",
        "calls_per_minute": 150,
        "news_calls_per_minute": 30,
        "delayed_after_seconds": 900,
    },
    "fred": {
        "calls_per_minute": 60,
    },
    "finnhub": {
        "calls_per_minute": 50,
    },
    "edgar": {
        "calls_per_second": 8,
    },
    "news": {
        "refresh_seconds": 600,
        "lookback_days": 7,
        "filings_lookback_days": 30,
        "per_source_limit": 50,
        "relationships_path": "relationships.yaml",
        "market_symbols": ["SPY", "QQQ", "TLT"],
        "reactions_per_refresh": 20,
        "dedupe_hours": 48,
        "dedupe_similarity": 0.7,
    },
    "sentiment": {
        "enabled": True,
        "model": "ProsusAI/finbert",
        "batch_size": 16,
    },
    "quant": {
        "refresh_seconds": 3600,
        "universe_path": "universe.yaml",
        "universe_ttl_seconds": 86400,
        "history_days": 400,
        "label_top_pct": 20,
        "label_bottom_pct": 20,
        "zscore_cap": 3,
        "weights": {"momentum": 0.30, "trend": 0.10, "value": 0.20, "quality": 0.25, "low_risk": 0.10, "news": 0.05},
        "news_halflife_days": 3,
        "news_min_headlines": 5,
        "news_window_days": 30,
        "universe_news": True,
        "signal_horizon_days": 63,
    },
    "cache": {
        "path": "data/terminal.db",
        "ttl_seconds": {
            "snapshot": 60,
            "clock": 60,
            "fred": 21600,
            "news": 600,
            "edgar": 3600,
            "bars": 3600,
            "peers": 86400,
            "cik": 604800,
            "metrics": 86400,
            "bars_daily": 300,
        },
    },
    "log": {
        "path": "logs/terminal.log",
    },
}


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Return base with override laid on top; nested dicts merge, everything else replaces."""
    out: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _positive(cfg: Mapping[str, Any], *path: str) -> None:
    node: Any = cfg
    for part in path:
        node = node[part]
    if not isinstance(node, (int, float)) or isinstance(node, bool) or node <= 0:
        raise ConfigError(f"{'.'.join(path)} must be a positive number, got {node!r}")


def validate(cfg: Mapping[str, Any]) -> None:
    _positive(cfg, "refresh_seconds")
    _positive(cfg, "alpaca", "calls_per_minute")
    _positive(cfg, "alpaca", "delayed_after_seconds")
    _positive(cfg, "alpaca", "news_calls_per_minute")
    _positive(cfg, "fred", "calls_per_minute")
    _positive(cfg, "finnhub", "calls_per_minute")
    _positive(cfg, "edgar", "calls_per_second")
    for name in ("refresh_seconds", "lookback_days", "filings_lookback_days", "per_source_limit", "reactions_per_refresh", "dedupe_hours"):
        _positive(cfg, "news", name)
    similarity = cfg["news"]["dedupe_similarity"]
    if not isinstance(similarity, (int, float)) or not 0 < similarity <= 1:
        raise ConfigError(f"news.dedupe_similarity must be between 0 and 1, got {similarity!r}")
    if not isinstance(cfg["news"]["relationships_path"], str) or not cfg["news"]["relationships_path"]:
        raise ConfigError("news.relationships_path must be a file path")
    market = cfg["news"]["market_symbols"]
    if not isinstance(market, list) or not all(isinstance(s, str) and s for s in market):
        raise ConfigError("news.market_symbols must be a list of ticker strings")
    if not isinstance(cfg["sentiment"]["enabled"], bool):
        raise ConfigError("sentiment.enabled must be true or false")
    _positive(cfg, "sentiment", "batch_size")
    for name in ("snapshot", "clock", "fred", "news", "edgar", "bars", "peers", "cik", "metrics", "bars_daily"):
        _positive(cfg, "cache", "ttl_seconds", name)
    for name in ("refresh_seconds", "universe_ttl_seconds", "history_days", "zscore_cap", "news_halflife_days", "news_min_headlines", "news_window_days", "signal_horizon_days"):
        _positive(cfg, "quant", name)
    for name in ("label_top_pct", "label_bottom_pct"):
        pct = cfg["quant"][name]
        if not isinstance(pct, (int, float)) or isinstance(pct, bool) or not 0 < pct <= 50:
            raise ConfigError(f"quant.{name} must be between 0 and 50, got {pct!r}")
    weights = cfg["quant"]["weights"]
    expected = ("momentum", "trend", "value", "quality", "low_risk", "news")
    if not isinstance(weights, Mapping) or set(weights) != set(expected):
        raise ConfigError(f"quant.weights must have exactly these keys: {', '.join(expected)}")
    if any(not isinstance(w, (int, float)) or isinstance(w, bool) or w < 0 for w in weights.values()) or sum(weights.values()) <= 0:
        raise ConfigError("quant.weights must be non-negative numbers that do not all equal zero")
    if not isinstance(cfg["quant"]["universe_news"], bool):
        raise ConfigError("quant.universe_news must be true or false")
    if not isinstance(cfg["quant"]["universe_path"], str) or not cfg["quant"]["universe_path"]:
        raise ConfigError("quant.universe_path must be a file path")
    equities = cfg["top_strip"]["equities"]
    if not isinstance(equities, list) or not all(isinstance(s, str) and s for s in equities):
        raise ConfigError("top_strip.equities must be a list of ticker strings")
    fred = cfg["top_strip"]["fred"]
    if not isinstance(fred, Mapping) or not all(isinstance(v, str) and v for v in fred.values()):
        raise ConfigError("top_strip.fred must map a label to a FRED series id")
    if cfg["alpaca"]["feed"] not in ("iex", "sip"):
        raise ConfigError("alpaca.feed must be iex or sip")


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    """Defaults, overlaid with config.yaml when it exists, validated."""
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path)
    if p.exists():
        with p.open(encoding="utf-8") as handle:
            user = yaml.safe_load(handle) or {}
        if not isinstance(user, Mapping):
            raise ConfigError(f"{p} must contain a mapping of settings, got {type(user).__name__}")
        # YAML reads the bare key 10Y as a string, but a key like 10 would be an int; normalise labels to str.
        if isinstance(user.get("top_strip"), Mapping) and isinstance(user["top_strip"].get("fred"), Mapping):
            user = deep_merge(user, {"top_strip": {"fred": {str(k): v for k, v in user["top_strip"]["fred"].items()}}})
        cfg = deep_merge(cfg, user)
    validate(cfg)
    return cfg


ENV_NAMES = {
    "alpaca_key_id": "ALPACA_PAPER_KEY_ID",
    "alpaca_secret": "ALPACA_PAPER_SECRET_KEY",
    "finnhub": "FINNHUB_API_KEY",
    "fred": "FRED_API_KEY",
    "edgar_user_agent": "EDGAR_USER_AGENT",
}


@dataclass(frozen=True, repr=False)
class Keys:
    alpaca_key_id: str = ""
    alpaca_secret: str = ""
    finnhub: str = ""
    fred: str = ""
    edgar_user_agent: str = ""

    @property
    def has_alpaca(self) -> bool:
        return bool(self.alpaca_key_id and self.alpaca_secret)

    @property
    def has_fred(self) -> bool:
        return bool(self.fred)

    @property
    def has_finnhub(self) -> bool:
        return bool(self.finnhub)

    @property
    def has_edgar(self) -> bool:
        # The placeholder from .env.example does not count as a real contact.
        return bool(self.edgar_user_agent) and "example.com" not in self.edgar_user_agent

    def summary(self) -> dict[str, bool]:
        """Which keys are present. Names only, never values: safe to print."""
        return {
            "alpaca": self.has_alpaca,
            "finnhub": self.has_finnhub,
            "fred": self.has_fred,
            "edgar_user_agent": self.has_edgar,
        }

    def __repr__(self) -> str:
        parts = ", ".join(f"{k}={'set' if v else 'unset'}" for k, v in self.summary().items())
        return f"Keys({parts})"

    __str__ = __repr__


def load_keys(env_path: str | Path = ".env") -> Keys:
    """Read the keys from .env (if present) and the environment. Values stay inside the Keys object."""
    load_dotenv(env_path, override=False)
    values = {field: os.environ.get(env_name, "").strip() for field, env_name in ENV_NAMES.items()}
    return Keys(**values)

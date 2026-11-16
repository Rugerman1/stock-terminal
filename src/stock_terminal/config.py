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
        "delayed_after_seconds": 900,
    },
    "fred": {
        "calls_per_minute": 60,
    },
    "cache": {
        "path": "data/terminal.db",
        "ttl_seconds": {"snapshot": 60, "clock": 60, "fred": 21600},
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
    _positive(cfg, "fred", "calls_per_minute")
    for name in ("snapshot", "clock", "fred"):
        _positive(cfg, "cache", "ttl_seconds", name)
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

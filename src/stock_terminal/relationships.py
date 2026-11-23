"""The relationship map (relationships.yaml).

For each ticker: its sector ETF, its peers, the macro topics that matter to it, and the
words that name the company in a headline. Plus three keyword tables: sector keywords,
macro topic keywords and catalyst keywords. Keywords match whole words, any case, in
the headline only, so every tag the app shows can be traced to a word in the file.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

log = logging.getLogger(__name__)

# Catalyst types from the spec, in the order they are shown. "other" is the honest fallback.
CATALYST_NAMES = {
    "earnings": "earnings",
    "guidance": "guidance",
    "analyst": "analyst rating",
    "mna": "M&A",
    "legal": "legal/regulatory",
    "product": "product",
    "insider": "insider",
    "macro": "macro",
    "other": "other",
}


@dataclass
class TickerMap:
    ticker: str
    sector_etf: Optional[str] = None
    peers: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    listed: bool = True   # False when the ticker has no entry in the file

    @property
    def symbols(self) -> list[str]:
        """The ticker, its peers and its ETF: everything whose news is pulled for this ticker."""
        out = [self.ticker] + list(self.peers)
        if self.sector_etf:
            out.append(self.sector_etf)
        return list(dict.fromkeys(out))


class KeywordTable:
    """name -> keywords, compiled once. `first(text)` returns the first name with a hit, `all(text)` every name."""

    def __init__(self, table: Mapping[str, list[str]]) -> None:
        self.names: list[str] = []
        self._patterns: dict[str, re.Pattern[str]] = {}
        for name, words in table.items():
            words = [str(w).strip() for w in (words or []) if str(w).strip()]
            if not words:
                continue
            self.names.append(str(name))
            self._patterns[str(name)] = compile_keywords(words)

    def first(self, text: str) -> Optional[str]:
        for name in self.names:
            if self._patterns[name].search(text):
                return name
        return None

    def all(self, text: str) -> list[str]:
        return [name for name in self.names if self._patterns[name].search(text)]


def compile_keywords(words: list[str]) -> re.Pattern[str]:
    """One regex that matches any of the words as a whole word, any case."""
    alternatives = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alternatives})(?![A-Za-z0-9])", re.IGNORECASE)


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(v).strip() for v in value if str(v).strip()]


class Relationships:
    def __init__(self, data: Optional[Mapping[str, Any]] = None, path: Optional[Path] = None) -> None:
        data = data or {}
        self.path = path
        self.tickers: dict[str, TickerMap] = {}
        for raw_ticker, spec in (data.get("tickers") or {}).items():
            ticker = str(raw_ticker).upper()
            spec = spec or {}
            etf = spec.get("sector_etf")
            self.tickers[ticker] = TickerMap(
                ticker=ticker,
                sector_etf=str(etf).upper() if etf else None,
                peers=[p.upper() for p in _str_list(spec.get("peers")) if p.upper() != ticker][:5],
                topics=_str_list(spec.get("topics")),
                aliases=_str_list(spec.get("aliases")),
            )
        sectors = data.get("sectors") or {}
        self.sector_names: dict[str, str] = {str(k).upper(): str((v or {}).get("name") or k) for k, v in sectors.items()}
        self.sectors = KeywordTable({str(k).upper(): _str_list((v or {}).get("keywords")) for k, v in sectors.items()})
        self.topics = KeywordTable({str(k): _str_list(v) for k, v in (data.get("topics") or {}).items()})
        self.catalysts = KeywordTable({str(k): _str_list(v) for k, v in (data.get("catalysts") or {}).items()})
        unknown = [t for t in self.catalysts.names if t not in CATALYST_NAMES]
        if unknown:
            log.warning("relationships: catalyst types not in the spec's list are kept but shown as-is: %s", unknown)
        self._alias_patterns: dict[str, re.Pattern[str]] = {}

    @classmethod
    def load(cls, path: str | Path) -> "Relationships":
        p = Path(path)
        if not p.exists():
            log.warning("relationships file %s not found: only DIRECT and FILING news will be tagged", p)
            return cls({}, p)
        with p.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if not isinstance(data, Mapping):
            raise ValueError(f"{p} must contain a mapping (tickers, sectors, topics, catalysts)")
        return cls(data, p)

    def for_ticker(self, ticker: str) -> TickerMap:
        ticker = ticker.upper()
        if ticker in self.tickers:
            return self.tickers[ticker]
        return TickerMap(ticker=ticker, listed=False)

    def aliases_for(self, ticker: str) -> list[str]:
        """The ticker plus the words that name the company; used for DIRECT and PEER matching in headlines."""
        ticker = ticker.upper()
        extra = self.tickers[ticker].aliases if ticker in self.tickers else []
        return list(dict.fromkeys([ticker] + extra))

    def mentions(self, text: str, ticker: str) -> bool:
        ticker = ticker.upper()
        pattern = self._alias_patterns.get(ticker)
        if pattern is None:
            pattern = compile_keywords(self.aliases_for(ticker))
            self._alias_patterns[ticker] = pattern
        return bool(pattern.search(text))

    def catalyst_of(self, text: str) -> str:
        return self.catalysts.first(text) or "other"

    def topics_in(self, text: str) -> list[str]:
        return self.topics.all(text)

    def sectors_in(self, text: str) -> list[str]:
        return self.sectors.all(text)

    def sector_name(self, etf: Optional[str]) -> str:
        if not etf:
            return ""
        return self.sector_names.get(etf.upper(), etf.upper())

    def all_symbols(self, tickers: list[str]) -> list[str]:
        out: list[str] = []
        for t in tickers:
            out.extend(self.for_ticker(t).symbols)
        return list(dict.fromkeys(out))


def catalyst_label(key: str) -> str:
    return CATALYST_NAMES.get(key, key)


def peers_snippet(ticker: str, peers: list[str], sector_etf: str = "???") -> str:
    """A YAML block for relationships.yaml, printed by --peers so Tyler can paste it."""
    peers = [p for p in peers if p.upper() != ticker.upper()][:5]
    lines = [
        f"  {ticker.upper()}:",
        f"    sector_etf: {sector_etf}",
        f"    peers: [{', '.join(peers)}]",
        "    topics: [rates]",
        f"    aliases: []",
    ]
    return "\n".join(lines)

"""The watchlist: a list of tickers saved in watchlist.yaml.

Tickers are normalised to upper case, validated against the shape of a US listing
symbol, de-duplicated, and written back to the file on every change so the app and
a hand edit never disagree for long.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)

# Letters, digits, a dot or a hyphen (BRK.B, BF-B), 1 to 10 characters, must start with a letter.
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

HEADER = "# Your watchlist. Edit by hand, or in the app with a (add) and d (delete). Saved on every change.\n"


def normalise(raw: str) -> str:
    return (raw or "").strip().upper()


def is_valid(ticker: str) -> bool:
    return bool(TICKER_RE.match(ticker))


class Watchlist:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.tickers: list[str] = []
        self.load()

    def load(self) -> None:
        self.tickers = []
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        raw = data.get("tickers") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            log.warning("%s has no 'tickers' list; starting empty", self.path)
            return
        for item in raw:
            ticker = normalise(str(item))
            if not is_valid(ticker):
                log.warning("skipping invalid ticker %r in %s", item, self.path)
                continue
            if ticker not in self.tickers:
                self.tickers.append(ticker)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump({"tickers": list(self.tickers)}, default_flow_style=False, sort_keys=False)
        self.path.write_text(HEADER + body, encoding="utf-8")

    def add(self, raw: str) -> Optional[str]:
        """Add a ticker. Returns the normalised ticker, or None if it was invalid or already present."""
        ticker = normalise(raw)
        if not is_valid(ticker) or ticker in self.tickers:
            return None
        self.tickers.append(ticker)
        self.save()
        return ticker

    def remove(self, raw: str) -> bool:
        ticker = normalise(raw)
        if ticker not in self.tickers:
            return False
        self.tickers.remove(ticker)
        self.save()
        return True

    def __contains__(self, raw: str) -> bool:
        return normalise(raw) in self.tickers

    def __len__(self) -> int:
        return len(self.tickers)

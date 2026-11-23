"""The dates strip: Fed and CPI dates from a hand-kept file, earnings from Finnhub.

Finnhub's economic calendar is a paid tier, so calendar.yaml holds the FOMC and CPI
dates with the page they were read from and the day they were read. Only dates read
from the source pages go in; nothing is typed from memory.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

from ..datapoint import DataPoint

log = logging.getLogger(__name__)


@dataclass
class Event:
    date: date
    name: str
    end: Optional[date] = None
    source: str = ""

    def label(self, today: date) -> str:
        days = (self.date - today).days
        when = self.date.strftime("%b %d").replace(" 0", " ")
        if self.end and self.end != self.date:
            when += f"-{self.end.day}"
        return f"{self.name} {when} ({days}d)"


def load_events(path: str | Path) -> tuple[list[Event], str]:
    """(events, note). A missing or broken file is a note on screen, not a crash."""
    p = Path(path)
    if not p.exists():
        return [], f"{p} not found: no Fed or CPI dates"
    try:
        with p.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception as exc:  # noqa: BLE001
        return [], f"{p} unreadable: {exc}"
    events: list[Event] = []
    for raw in data.get("events") or []:
        try:
            start = raw["date"] if isinstance(raw["date"], date) else date.fromisoformat(str(raw["date"]))
            end = raw.get("end")
            end = end if isinstance(end, date) or end is None else date.fromisoformat(str(end))
            events.append(Event(start, str(raw["name"]), end, str(raw.get("source", ""))))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("calendar entry skipped: %r (%s)", raw, exc)
    events.sort(key=lambda e: e.date)
    return events, ""


def upcoming(events: list[Event], today: date, limit: int = 3) -> list[Event]:
    return [e for e in events if (e.end or e.date) >= today][:limit]


def dates_strip(events: list[Event], earnings: dict[str, DataPoint], today: date, limit: int = 3, note: str = "") -> str:
    parts = [e.label(today) for e in upcoming(events, today, limit)]
    for ticker, dp in earnings.items():
        if dp.ok and dp.as_of is not None:
            days = (dp.as_of.date() - today).days
            parts.append(f"{ticker} earnings {dp.as_of.strftime('%b %d').replace(' 0', ' ')} ({days}d)")
    text = "Dates: " + ("  |  ".join(parts) if parts else "none in the file or the calendar")
    if note:
        text += f"  [{note}]"
    return text

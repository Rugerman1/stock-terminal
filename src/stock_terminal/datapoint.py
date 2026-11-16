"""One number, with its provenance.

Every value the app shows travels inside a DataPoint, so the screen can always say
where a number came from, how old it is, and whether it is fresh, stale, delayed or
missing. The spec's rule is "never invent data": a missing number is shown as
missing with the reason, never as zero or a placeholder.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

FRESH = "fresh"      # fetched from the source just now, or served from cache inside its TTL
STALE = "stale"      # the source failed; this is the last value we had, past its TTL
DELAYED = "delayed"  # fetched fine, but the data itself is older than it should be
MISSING = "missing"  # nothing to show; `note` says why

STATUSES = (FRESH, STALE, DELAYED, MISSING)

# Suffixes the screen appends so a stale or delayed number can never pass as fresh.
STALE_MARK = "*"
DELAYED_MARK = "~"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DataPoint:
    value: Any                          # None only when status is MISSING
    source: str                         # e.g. "alpaca/iex", "fred/VIXCLS"
    status: str                         # one of STATUSES
    as_of: Optional[datetime] = None    # the timestamp of the data itself (trade time, observation date)
    fetched_at: Optional[datetime] = None  # when we last reached the source
    note: str = ""                      # the reason behind stale / delayed / missing

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}")
        if self.value is None and self.status != MISSING:
            raise ValueError("a DataPoint without a value must have status MISSING")
        if self.value is not None and self.status == MISSING:
            raise ValueError("a MISSING DataPoint cannot carry a value")

    @classmethod
    def missing(cls, source: str, note: str) -> "DataPoint":
        return cls(value=None, source=source, status=MISSING, note=note)

    @property
    def ok(self) -> bool:
        return self.value is not None

    def age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        """Seconds between the data's own timestamp and now. None when the data has no timestamp."""
        if self.as_of is None:
            return None
        now = now or utcnow()
        return (now - self.as_of).total_seconds()

    def display(self, fmt: str = "{:.2f}") -> str:
        """The number as text, with the stale or delayed mark attached. Missing shows as --."""
        if self.value is None:
            return "--"
        try:
            text = fmt.format(self.value)
        except (ValueError, TypeError):
            text = str(self.value)
        if self.status == STALE:
            text += STALE_MARK
        elif self.status == DELAYED:
            text += DELAYED_MARK
        return text

    def status_label(self) -> str:
        """Short text for a status column: empty when fresh, otherwise STATUS: reason."""
        if self.status == FRESH:
            return ""
        return self.status.upper() + (f": {self.note}" if self.note else "")


def human_age(seconds: Optional[float]) -> str:
    """Turn an age in seconds into text like 42s, 7m, 3h, 2d. Unknown ages show as ?."""
    if seconds is None:
        return "?"
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"

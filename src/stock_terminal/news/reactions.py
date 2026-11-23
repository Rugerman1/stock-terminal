"""Filling a reaction from price bars. Pure functions, so the rules are testable without a network.

The rules, in plain words:
- p0 is the first minute bar at or after the headline. A headline that lands after the
  close gets the next session's first bar, so "the price then" is the first price anyone
  could have traded at.
- p1h is the first minute bar at or after p0's time plus one hour.
- p1d is the close of the first full session after p0's session; p5d the close of the
  fifth. Sessions are counted from the daily bars Alpaca returns, so holidays take care
  of themselves.
A slot stays empty until its time has passed and a bar exists; nothing is interpolated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .models import Reaction, parse_when

NY = ZoneInfo("America/New_York")
GIVE_UP_AFTER = timedelta(days=21)   # a headline this old with no bars will never get them
RETRY_AFTER = timedelta(minutes=30)  # how long to wait before asking for bars again when a slot is not filled yet


@dataclass(frozen=True)
class Bar:
    at: datetime     # aware UTC
    close: float

    @property
    def session(self) -> date:
        return self.at.astimezone(NY).date()


def bars_from_payload(payload: Optional[list[dict]]) -> list[Bar]:
    out: list[Bar] = []
    for raw in payload or []:
        at = parse_when(raw.get("t"))
        close = raw.get("c")
        if at is None or close is None:
            continue
        out.append(Bar(at, float(close)))
    out.sort(key=lambda b: b.at)
    return out


def first_at_or_after(bars: list[Bar], when: datetime) -> Optional[Bar]:
    for bar in bars:
        if bar.at >= when:
            return bar
    return None


def nth_session_close(daily: list[Bar], after: date, n: int) -> Optional[Bar]:
    """The close of the n-th session strictly after the given date (n = 1 is the next session)."""
    sessions = [b for b in daily if b.session > after]
    sessions.sort(key=lambda b: b.session)
    return sessions[n - 1] if len(sessions) >= n else None


def minute_window(reaction: Reaction) -> tuple[datetime, datetime]:
    """The minute bars worth asking for: from the headline to four days after it (covers a long weekend)."""
    start = reaction.published_at
    return start, start + timedelta(days=4)


def daily_window(reaction: Reaction) -> tuple[datetime, datetime]:
    start = reaction.published_at - timedelta(days=1)
    return start, start + timedelta(days=14)


def needs_minutes(reaction: Reaction) -> bool:
    return reaction.p0 is None or reaction.p1h is None


def needs_daily(reaction: Reaction) -> bool:
    return reaction.p1d is None or reaction.p5d is None


def fill(reaction: Reaction, minutes: Optional[list[Bar]], daily: Optional[list[Bar]], now: datetime) -> Reaction:
    """Fill whatever the bars allow. `minutes` / `daily` may be None when they were not fetched this time."""
    if minutes is not None:
        if reaction.p0 is None:
            bar = first_at_or_after(minutes, reaction.published_at)
            if bar is not None:
                reaction.p0, reaction.p0_at = bar.close, bar.at
        if reaction.p0_at is not None and reaction.p1h is None:
            bar = first_at_or_after(minutes, reaction.p0_at + timedelta(hours=1))
            if bar is not None:
                reaction.p1h, reaction.p1h_at = bar.close, bar.at
    if daily is not None and reaction.p0_at is not None:
        session0 = Bar(reaction.p0_at, reaction.p0 or 0.0).session
        if reaction.p1d is None:
            bar = nth_session_close(daily, session0, 1)
            if bar is not None:
                reaction.p1d, reaction.p1d_at = bar.close, bar.at
        if reaction.p5d is None:
            bar = nth_session_close(daily, session0, 5)
            if bar is not None:
                reaction.p5d, reaction.p5d_at = bar.close, bar.at

    missing = [k for k in ("p0", "p1h", "p1d", "p5d") if getattr(reaction, k) is None]
    if not missing:
        reaction.status, reaction.note, reaction.next_try_at = "done", "", None
    elif now - reaction.published_at > GIVE_UP_AFTER:
        reaction.status = "failed"
        reaction.note = f"no bars for {', '.join(missing)} within {GIVE_UP_AFTER.days} days"
        reaction.next_try_at = None
    else:
        reaction.status = "pending"
        reaction.note = f"waiting for {', '.join(missing)}"
        reaction.next_try_at = now + RETRY_AFTER
    return reaction


def describe(reaction: Optional[Reaction]) -> str:
    """One short line for the screen: +1h +0.4%  +1d -1.2%  +5d ..."""
    if reaction is None:
        return "reaction: not tracked"
    parts = []
    for label, key in (("+1h", "p1h"), ("+1d", "p1d"), ("+5d", "p5d")):
        move = reaction.move(key)
        parts.append(f"{label} {move:+.1f}%" if move is not None else f"{label} ...")
    if reaction.status == "failed":
        return "reaction: no bars (" + reaction.note + ")"
    return "reaction " + "  ".join(parts)

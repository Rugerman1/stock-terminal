from datetime import datetime, timedelta, timezone

from stock_terminal.news.models import DIRECT, Reaction
from stock_terminal.news.reactions import Bar, bars_from_payload, daily_window, describe, fill, minute_window, nth_session_close

UTC = timezone.utc


def minute_bars(day: datetime, start_price: float = 100.0):
    """One session of minute bars, 9:30 to 16:00 ET (14:30 to 21:00 UTC in November), price rising 0.01 a minute."""
    open_at = day.replace(hour=14, minute=30, tzinfo=UTC)
    return [Bar(open_at + timedelta(minutes=i), start_price + 0.01 * i) for i in range(390)]


def daily_bar(day: datetime, close: float) -> Bar:
    return Bar(day.replace(hour=5, minute=0, tzinfo=UTC), close)   # Alpaca stamps daily bars at midnight New York


def reaction(published: datetime) -> Reaction:
    return Reaction("n:1", "AAPL", DIRECT, "earnings", published)


def test_bars_from_payload_sorts_and_skips_broken_rows():
    bars = bars_from_payload([{"t": "2026-11-17T15:00:00+00:00", "c": 2}, {"t": "2026-11-17T14:30:00Z", "c": 1}, {"t": None, "c": 3}, {"t": "x", "c": 4}])
    assert [b.close for b in bars] == [1.0, 2.0]


def test_pre_open_headline_uses_first_bar_and_one_hour_later():
    tue = datetime(2026, 11, 17, tzinfo=UTC)
    published = tue.replace(hour=14, minute=0)                 # 9:00 AM ET, before the open
    r = fill(reaction(published), minute_bars(tue), None, now=published + timedelta(hours=3))
    assert r.p0 == 100.0 and r.p0_at == tue.replace(hour=14, minute=30)
    assert abs(r.p1h - 100.60) < 1e-9 and r.p1h_at == tue.replace(hour=15, minute=30)
    assert r.status == "pending" and r.note == "waiting for p1d, p5d" and r.next_try_at is not None


def test_after_hours_headline_waits_for_the_next_open():
    tue = datetime(2026, 11, 17, tzinfo=UTC)
    wed = datetime(2026, 11, 18, tzinfo=UTC)
    published = tue.replace(hour=22, minute=0)                 # 5:00 PM ET, after the close
    r = fill(reaction(published), minute_bars(tue) + minute_bars(wed, 110.0), None, now=published + timedelta(hours=20))
    assert r.p0 == 110.0 and r.p0_at == wed.replace(hour=14, minute=30)
    assert abs(r.p1h - 110.60) < 1e-9


def test_daily_slots_count_sessions_after_p0_session():
    tue = datetime(2026, 11, 17, tzinfo=UTC)
    r = reaction(tue.replace(hour=14))
    r.p0, r.p0_at = 100.0, tue.replace(hour=14, minute=30)
    daily = [daily_bar(datetime(2026, 11, d, tzinfo=UTC), close) for d, close in ((16, 98.0), (17, 101.0), (18, 102.0), (19, 103.0), (20, 104.0), (23, 105.0), (24, 106.0))]
    r = fill(r, None, daily, now=tue + timedelta(days=10))
    assert r.p1d == 102.0 and r.p1d_at.date().isoformat() == "2026-11-18"   # next session, the weekend skipped by the bars themselves
    assert r.p5d == 106.0 and r.p5d_at.date().isoformat() == "2026-11-24"
    assert nth_session_close(daily, tue.date(), 6) is None


def test_old_headline_with_no_bars_fails_honestly():
    published = datetime(2026, 10, 1, 14, tzinfo=UTC)
    r = fill(reaction(published), [], [], now=published + timedelta(days=30))
    assert r.status == "failed" and "p0" in r.note and r.next_try_at is None


def test_windows_and_describe():
    published = datetime(2026, 11, 17, 14, tzinfo=UTC)
    r = reaction(published)
    assert minute_window(r) == (published, published + timedelta(days=4))
    assert daily_window(r)[0] == published - timedelta(days=1)
    assert describe(None) == "reaction: not tracked"
    assert describe(r) == "reaction +1h ...  +1d ...  +5d ..."
    r.p0, r.p1h, r.p1d = 100.0, 100.5, 98.0
    assert describe(r) == "reaction +1h +0.5%  +1d -2.0%  +5d ..."
    r.status, r.note = "failed", "no bars for p5d within 21 days"
    assert describe(r) == "reaction: no bars (no bars for p5d within 21 days)"

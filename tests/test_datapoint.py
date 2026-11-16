from datetime import timedelta

import pytest

from stock_terminal.datapoint import DELAYED, FRESH, MISSING, STALE, DataPoint, human_age, utcnow


def test_missing_cannot_carry_a_value():
    with pytest.raises(ValueError):
        DataPoint(1.0, "src", MISSING)


def test_value_required_unless_missing():
    with pytest.raises(ValueError):
        DataPoint(None, "src", FRESH)


def test_unknown_status_rejected():
    with pytest.raises(ValueError):
        DataPoint(1.0, "src", "guess")


def test_display_marks_each_status():
    assert DataPoint(1.5, "s", FRESH).display() == "1.50"
    assert DataPoint(1.5, "s", STALE).display() == "1.50*"
    assert DataPoint(1.5, "s", DELAYED).display() == "1.50~"
    assert DataPoint.missing("s", "no key").display() == "--"
    assert DataPoint(0.5, "s", FRESH).display("{:+.2f}%") == "+0.50%"


def test_display_falls_back_for_non_numeric():
    assert DataPoint({"a": 1}, "s", FRESH).display() == "{'a': 1}"


def test_status_label():
    assert DataPoint(1.0, "s", FRESH).status_label() == ""
    assert DataPoint(1.0, "s", STALE, note="boom").status_label() == "STALE: boom"
    assert DataPoint.missing("s", "no key").status_label() == "MISSING: no key"


def test_age_seconds():
    now = utcnow()
    dp = DataPoint(1.0, "s", FRESH, as_of=now - timedelta(seconds=30))
    assert 29.9 <= dp.age_seconds(now) <= 30.1
    assert DataPoint(1.0, "s", FRESH).age_seconds() is None


def test_human_age():
    assert human_age(None) == "?"
    assert human_age(5) == "5s"
    assert human_age(125) == "2m"
    assert human_age(7200) == "2h"
    assert human_age(90_000) == "1d"
    assert human_age(-3) == "0s"

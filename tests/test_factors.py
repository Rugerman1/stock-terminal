import math
from datetime import date, timedelta
from statistics import stdev

from stock_terminal.quant.factors import (
    low_risk, mean_std, momentum, news_sentiment, quality_components, realized_volatility, trend, value, zscore,
)

AAPL_METRICS = {"epsTTM": 8.9745, "pfcfShareTTM": 36.5889, "roeTTM": 127.61, "grossMarginTTM": 48.79, "totalDebt/totalEquityAnnual": 0.7277}


def closes(n: int, start: float = 100.0, growth: float = 1.001):
    first = date(2025, 1, 1)
    return [((first + timedelta(days=i)).isoformat(), start * growth ** i) for i in range(n)]


def test_momentum_uses_the_21st_and_252nd_closes_back():
    f = momentum(closes(260))
    assert f.ok and abs(f.value - (1.001 ** 231 - 1.0)) < 1e-12
    assert f.inputs["price_21d_ago_date"] == closes(260)[-22][0] and f.inputs["price_252d_ago_date"] == closes(260)[-253][0]
    short = momentum(closes(200))
    assert not short.ok and "needs 253" in short.note


def test_trend_against_the_200_day_average():
    flat = trend(closes(250, growth=1.0))
    assert flat.ok and abs(flat.value) < 1e-12
    rising = trend(closes(250))
    assert rising.ok and rising.value > 0 and rising.inputs["avg_200d"] < rising.inputs["price"]
    assert not trend(closes(199)).ok


def test_realized_volatility_and_low_risk():
    series = closes(300, growth=1.0)
    assert realized_volatility(series) == 0.0 and low_risk(series).value == 0.0
    # Alternating +1% / -1% days: the vol is the sample std of those log returns, annualised.
    prices = [(f"2025-01-{i + 1:02d}" if i < 28 else f"2025-02-{i - 27:02d}", 100.0 * (1.01 if i % 2 else 1.0)) for i in range(400)]
    returns = [math.log(prices[i][1] / prices[i - 1][1]) for i in range(len(prices) - 252, len(prices))]
    expected = stdev(returns) * math.sqrt(252)
    assert abs(realized_volatility(prices) - expected) < 1e-12
    assert abs(low_risk(prices).value + expected) < 1e-12
    assert not low_risk(closes(100)).ok


def test_value_is_the_average_of_the_two_yields():
    f = value(AAPL_METRICS, 343.40)
    ey, fy = 8.9745 / 343.40, 1.0 / 36.5889
    assert f.ok and abs(f.value - (ey + fy) / 2) < 1e-12 and f.note == ""
    assert abs(f.inputs["earnings_yield"] - ey) < 1e-12 and abs(f.inputs["fcf_yield"] - fy) < 1e-12
    only_eps = value({"epsTTM": 5.0}, 100.0)
    assert only_eps.ok and abs(only_eps.value - 0.05) < 1e-12 and "only one" in only_eps.note
    assert not value(None, 100.0).ok and not value({"pfcfShareTTM": 0}, 100.0).ok
    assert value({"epsTTM": -2.0}, 100.0).value == -0.02          # a loss is a negative yield, shown as such


def test_quality_components_and_fallbacks():
    parts = quality_components(AAPL_METRICS)
    assert parts["roe"].value == 127.61 and parts["gross_margin"].value == 48.79 and parts["neg_debt_to_equity"].value == -0.7277
    quarterly = quality_components({"totalDebt/totalEquityQuarterly": 0.5})
    assert quarterly["neg_debt_to_equity"].value == -0.5 and not quarterly["roe"].ok and "no roeTTM" in quarterly["roe"].note
    empty = quality_components(None)
    assert all(not p.ok for p in empty.values())


def test_news_sentiment_weights_halve_every_three_days():
    f = news_sentiment([(0.0, 1.0), (3.0, 0.0), (6.0, -1.0)], halflife_days=3.0, min_headlines=3)
    assert f.ok and abs(f.value - (1.0 - 0.25) / 1.75) < 1e-12 and f.inputs["headlines"] == 3
    few = news_sentiment([(0.0, 1.0), (1.0, 1.0)], min_headlines=5)
    assert not few.ok and "have 2" in few.note
    assert news_sentiment([(0.0, None), (1.0, 0.5)], min_headlines=1).inputs["headlines"] == 1


def test_mean_std_and_capped_zscore():
    avg, sd = mean_std([1.0, 2.0, 3.0, 4.0])
    assert avg == 2.5 and abs(sd - stdev([1, 2, 3, 4])) < 1e-12
    assert mean_std([5.0]) == (5.0, None) and mean_std([]) == (None, None)
    assert zscore(10.0, 2.5, sd) == 3.0 and zscore(-10.0, 2.5, sd) == -3.0
    assert abs(zscore(3.0, 2.5, sd) - 0.5 / sd) < 1e-12
    assert zscore(1.0, 2.5, 0.0) is None and zscore(None, 2.5, sd) is None and zscore(1.0, None, sd) is None

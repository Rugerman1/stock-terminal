import math

from stock_terminal.quant import blackscholes as bs


def test_spec_values_call_put_delta():
    assert round(bs.call_price(100, 100, 1, 0.05, 0.20), 4) == 10.4506
    assert round(bs.put_price(100, 100, 1, 0.05, 0.20), 4) == 5.5735
    assert round(bs.delta("call", 100, 100, 1, 0.05, 0.20), 4) == 0.6368
    assert round(bs.delta("put", 100, 100, 1, 0.05, 0.20), 4) == round(0.6368 - 1.0, 4)


def test_spec_value_expected_move():
    assert round(bs.expected_move(100, 0.30, 30), 4) == 8.6007


def test_put_call_parity_and_greeks():
    S, K, T, r, s = 120.0, 110.0, 0.5, 0.04, 0.35
    assert abs((bs.call_price(S, K, T, r, s) - bs.put_price(S, K, T, r, s)) - (S - K * math.exp(-r * T))) < 1e-9
    assert bs.gamma(S, K, T, r, s) > 0 and bs.vega(S, K, T, r, s) > 0
    assert bs.theta("call", S, K, T, r, s) < 0 and bs.theta("put", S, K, T, r, s) < 0
    assert bs.price("put", S, K, T, r, s) == bs.put_price(S, K, T, r, s)


def test_implied_vol_round_trips_and_rejects_impossible_prices():
    assert abs(bs.implied_vol("call", 10.4506, 100, 100, 1, 0.05) - 0.20) < 1e-4
    assert abs(bs.implied_vol("put", 5.5735, 100, 100, 1, 0.05) - 0.20) < 1e-4
    assert bs.implied_vol("call", 0.5, 100, 100, 1, 0.05) is None        # below the intrinsic-ish floor at 1% vol
    assert bs.implied_vol("call", 100.5, 100, 100, 1, 0.05) is None      # a call can never be worth more than the stock
    assert bs.implied_vol("call", 0.0, 100, 100, 1, 0.05) is None


def test_breakeven_and_probabilities():
    assert bs.breakeven("call", 100, 4.0) == 104.0 and bs.breakeven("put", 100, 4.0) == 96.0
    p_call = bs.prob_beyond("call", 100, 104, 45 / 365, 0.25)
    p_put = bs.prob_beyond("put", 100, 96, 45 / 365, 0.25)
    assert 0.25 < p_call < 0.45 and 0.25 < p_put < 0.45
    # Zero drift lognormal: the median end price is below the start, so ending above the start is under 50%.
    assert bs.prob_beyond("call", 100, 100, 1, 0.5) < 0.5
    # A positive drift lifts the call's odds.
    assert bs.prob_beyond("call", 100, 104, 45 / 365, 0.25, drift=0.05) > p_call


def test_simulated_expected_value_is_reproducible_and_near_the_closed_form():
    ev1 = bs.expected_value("call", 100, 100, 45 / 365, 0.25, 4.0)
    ev2 = bs.expected_value("call", 100, 100, 45 / 365, 0.25, 4.0)
    assert ev1 == ev2                                                      # fixed seed
    fair = bs.call_price(100, 100, 45 / 365, 0.0, 0.25) * 100 - 400        # zero-drift closed form, per contract
    assert abs(ev1 - fair) < 8.0                                           # Monte Carlo noise on 20,000 paths
    assert bs.expected_value("put", 100, 100, 45 / 365, 0.25, 4.0, n=5000, seed=1) != ev1

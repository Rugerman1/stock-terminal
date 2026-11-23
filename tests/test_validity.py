from datetime import datetime, timedelta, timezone

from stock_terminal.quant.validity import (
    RANDOM_LIKE, SIGNIFICANT, UNPROVEN, WEAK, ScoredSignal, Trade, alternate, bef, build_report, monte_carlo, paired_outcomes,
    prob_at_least, prob_at_most, prob_behind, prob_more_than, status_for, validity_summary,
)

CFG = {"min_signals": 30, "window": 50, "random_like_above": 0.05, "significant_below": 0.01, "monte_carlo_runs": 2000, "seed": 42}
T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def test_spec_binomial_values():
    assert [round(prob_behind(n, 0.6) * 100, 1) for n in (10, 50, 100)] == [16.6, 5.7, 1.7]
    assert round(prob_more_than(300, 500, 0.6) * 100, 1) == 48.3
    assert round(prob_more_than(250, 500, 0.4) * 100, 5) == 0.00025
    assert prob_at_least(0, 10, 0.3) == 1.0 and prob_at_most(10, 10, 0.3) == 1.0
    assert abs(prob_at_least(5, 10, 0.5) + prob_at_most(4, 10, 0.5) - 1.0) < 1e-12


def test_spec_bef_example():
    assert bef([1, 0, 1, 0, 1, 1, 0]) == (2, 2, 0.25)
    assert paired_outcomes([1, 1, 0, 1, 0, 0, 0]) == [1, 1, 0, 0, 0]
    assert bef([1, 0, 1, 0]) == (0, 0, None)
    assert bef([]) == (0, 0, None)


def test_alternate_inserts_opposite_test_trades_in_gaps():
    trades = [Trade(1, 1, T0, T0 + timedelta(days=10)), Trade(1, 0, T0 + timedelta(days=20), T0 + timedelta(days=30)), Trade(-1, 1, T0 + timedelta(days=30), T0 + timedelta(days=40))]
    seen = []

    def gap(direction, start, end):
        seen.append((direction, start, end))
        return 1

    out = alternate(trades, gap)
    assert [(t.direction, t.virtual) for t in out] == [(1, False), (-1, True), (1, False), (-1, False)]
    assert seen == [(-1, T0 + timedelta(days=10), T0 + timedelta(days=20))]
    # A gap that cannot be scored is left alone.
    assert len(alternate(trades, lambda d, a, b: None)) == 3


def test_status_thresholds():
    assert status_for(29, 0.001, CFG) == UNPROVEN and status_for(30, None, CFG) == UNPROVEN
    assert status_for(30, 0.2, CFG) == RANDOM_LIKE and status_for(30, 0.0501, CFG) == RANDOM_LIKE
    assert status_for(30, 0.03, CFG) == WEAK and status_for(30, 0.05, CFG) == WEAK
    assert status_for(30, 0.009, CFG) == SIGNIFICANT


def signals(n, wins, pool_rate=0.5, ticker="AAPL"):
    out = []
    for i in range(n):
        pool = [1] * int(round(pool_rate * 100)) + [0] * (100 - int(round(pool_rate * 100)))
        out.append(ScoredSignal(ticker if i % 2 == 0 else "MSFT", 1 if i % 3 else -1, 1 if i < wins else 0, T0 + timedelta(days=i * 7), T0 + timedelta(days=i * 7 + 90), pool))
    return out


def test_monte_carlo_is_reproducible_and_bounded():
    sure = monte_carlo(signals(10, 8, pool_rate=1.0), 8, 500, 1)
    assert sure.probability == 1.0                                     # every random name wins, so every run matches
    hopeless = monte_carlo(signals(10, 8, pool_rate=0.0), 8, 500, 1)
    assert hopeless.probability == 0.0
    a = monte_carlo(signals(20, 14), 14, 2000, 42)
    b = monte_carlo(signals(20, 14), 14, 2000, 42)
    assert a.probability == b.probability and 0.0 < a.probability < 0.2
    missing = monte_carlo([ScoredSignal("X", 1, 1, T0, T0, [])], 1, 10, 1)
    assert missing.probability is None and "no universe outcomes" in missing.note


def test_build_report_runs_the_three_tests():
    report = build_report("long_term", signals(30, 25), n_open=2, cfg=CFG)
    assert report.n_scored == 30 and report.binomial.n == 30 and report.binomial.k == 25
    assert report.binomial.probability < 0.001 and report.status == SIGNIFICANT and report.column_text == SIGNIFICANT
    assert report.binomial_empirical.baseline == 0.5 and report.monte_carlo.probability is not None
    assert set(report.per_ticker) == {"AAPL", "MSFT"} and set(report.bef) == {"AAPL", "MSFT"}
    assert report.bef["AAPL"].note.endswith("kept in same-result pairs")

    coin = build_report("long_term", signals(40, 21), 0, CFG)
    assert coin.status == RANDOM_LIKE and coin.virtual_mode and "Virtual mode" in validity_summary(coin)

    thin = build_report("options", signals(12, 10), 1, CFG)
    assert thin.status == UNPROVEN and thin.column_text == "UNPROVEN 12/30" and thin.binomial.n == 12

    empty = build_report("options", [], 0, CFG)
    assert empty.column_text == "UNPROVEN 0/30" and "no scored signals yet" in empty.notes and validity_summary(None) == "No validity report yet."


def test_window_keeps_the_newest_fifty():
    report = build_report("long_term", signals(80, 60), 0, CFG)
    assert report.binomial.n == 50 and report.n_scored == 80

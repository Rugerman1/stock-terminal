"""Black-Scholes and the probability sums the options check needs. Pure functions, nothing fetched.

Conventions: S spot, K strike, T years to expiry, r risk-free rate (decimal, continuous),
sigma annual volatility (decimal), no dividends. Spec test values: S = K = 100, T = 1,
r = 5 percent, sigma = 20 percent gives call 10.4506, put 5.5735, call delta 0.6368;
expected move at price 100, IV 30 percent, 30 days is 8.6007.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

SQRT_2 = math.sqrt(2.0)
DAYS_PER_YEAR = 365.0


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT_2))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        raise ValueError("S, K, T and sigma must be positive")
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return d1, d1 - sigma * math.sqrt(T)


def call_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)


def put_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def price(kind: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    return call_price(S, K, T, r, sigma) if kind == "call" else put_price(S, K, T, r, sigma)


def delta(kind: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return norm_cdf(d1) if kind == "call" else norm_cdf(d1) - 1.0


def gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return norm_pdf(d1) / (S * sigma * math.sqrt(T))


def vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Price change for a one-point (0.01) change in volatility, the way quote screens show it."""
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return S * norm_pdf(d1) * math.sqrt(T) / 100.0


def theta(kind: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Price change per calendar day as time passes (negative for a long option)."""
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    first = -S * norm_pdf(d1) * sigma / (2.0 * math.sqrt(T))
    if kind == "call":
        annual = first - r * K * math.exp(-r * T) * norm_cdf(d2)
    else:
        annual = first + r * K * math.exp(-r * T) * norm_cdf(-d2)
    return annual / DAYS_PER_YEAR


def implied_vol(kind: str, market_price: float, S: float, K: float, T: float, r: float, lo: float = 0.01, hi: float = 5.0) -> Optional[float]:
    """The volatility at which Black-Scholes matches the market price, by bisection. None when no vol in [1%, 500%] fits."""
    if market_price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(S - K * math.exp(-r * T), 0.0) if kind == "call" else max(K * math.exp(-r * T) - S, 0.0)
    if market_price < intrinsic or price(kind, S, K, T, r, hi) < market_price:
        return None
    if price(kind, S, K, T, r, lo) > market_price:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if price(kind, S, K, T, r, mid) > market_price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi)


def expected_move(S: float, iv: float, days: float) -> float:
    """price x IV x sqrt(days / 365): the one-standard-deviation move the market prices in over `days`."""
    return S * iv * math.sqrt(days / DAYS_PER_YEAR)


def breakeven(kind: str, K: float, premium: float) -> float:
    return K + premium if kind == "call" else K - premium


def prob_beyond(kind: str, S: float, level: float, T: float, sigma: float, drift: float = 0.0) -> float:
    """Probability a lognormal price ends above `level` (call) or below it (put) after T years.

    ln(S_T / S) ~ Normal((drift - sigma^2 / 2) T, sigma^2 T). drift = r gives the market's risk-neutral view;
    drift = 0 gives the model's "no edge in direction" view the spec asks for.
    """
    if S <= 0 or level <= 0 or T <= 0 or sigma <= 0:
        raise ValueError("S, level, T and sigma must be positive")
    z = (math.log(level / S) - (drift - 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    above = 1.0 - norm_cdf(z)
    return above if kind == "call" else 1.0 - above


def simulate_end_prices(S: float, T: float, sigma: float, n: int = 20_000, seed: int = 42, drift: float = 0.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    z = rng.standard_normal(n)
    return S * np.exp((drift - 0.5 * sigma * sigma) * T + sigma * math.sqrt(T) * z)


def expected_value(kind: str, S: float, K: float, T: float, sigma: float, premium: float, n: int = 20_000, seed: int = 42) -> float:
    """Average payoff over simulated end prices (zero drift) minus the premium, times 100 shares. Per contract, in dollars."""
    end = simulate_end_prices(S, T, sigma, n, seed)
    payoff = np.maximum(end - K, 0.0) if kind == "call" else np.maximum(K - end, 0.0)
    return float(payoff.mean() - premium) * 100.0

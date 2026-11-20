"""Black-Scholes prices and Greeks, standard library only.

Same formulas and units as skills/options-strategy-advisor/scripts/black_scholes.py
(tests/test_options_pricing.py checks they agree), rewritten without numpy and
scipy so the dashboard image stays small.

Units, all per share (multiply by 100 for one contract):
- delta: option price change per $1 move in the stock
- gamma: delta change per $1 move in the stock
- theta: option price change per calendar day
- vega:  option price change per 1 percentage point of volatility
- rho:   option price change per 1 percentage point of interest rate

Black-Scholes prices European options (exercise only at expiry). US stock
options are American (exercise any time), which matters little for calls on
stocks without a dividend and a bit more for deep in-the-money puts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


def intrinsic(right: str, S: float, K: float) -> float:
    return max(0.0, S - K) if right == "call" else max(0.0, K - S)


def greeks(
    right: str, S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0
) -> Greeks:
    """Price and Greeks for one option. T in years, r/sigma/q as decimals (0.04 = 4%)."""
    if S <= 0 or K <= 0:
        raise ValueError("Stock price and strike must be positive")
    if T <= 0 or sigma <= 0:
        # At expiry (or with no volatility) an option is worth its intrinsic value.
        value = intrinsic(right, S, K)
        itm = (S > K) if right == "call" else (S < K)
        delta = (1.0 if right == "call" else -1.0) if itm else 0.0
        return Greeks(value, delta, 0.0, 0.0, 0.0, 0.0)
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    disc_q = math.exp(-q * T)
    disc_r = math.exp(-r * T)
    pdf = norm_pdf(d1)
    gamma = disc_q * pdf / (S * sigma * sq)
    vega = S * disc_q * pdf * sq / 100
    decay = -S * pdf * sigma * disc_q / (2 * sq)
    if right == "call":
        price = S * disc_q * norm_cdf(d1) - K * disc_r * norm_cdf(d2)
        delta = disc_q * norm_cdf(d1)
        theta = (decay - r * K * disc_r * norm_cdf(d2) + q * S * norm_cdf(d1) * disc_q) / 365
        rho = K * T * disc_r * norm_cdf(d2) / 100
    else:
        price = K * disc_r * norm_cdf(-d2) - S * disc_q * norm_cdf(-d1)
        delta = disc_q * (norm_cdf(d1) - 1)
        theta = (decay + r * K * disc_r * norm_cdf(-d2) - q * S * norm_cdf(-d1) * disc_q) / 365
        rho = -K * T * disc_r * norm_cdf(-d2) / 100
    return Greeks(max(0.0, price), delta, gamma, theta, vega, rho)


def prob_above(S: float, level: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    """Model chance the stock ends above `level` at expiry (lognormal, risk-neutral drift)."""
    if level <= 0:
        return 1.0
    if T <= 0 or sigma <= 0:
        return 1.0 if S > level else 0.0
    sq = math.sqrt(T)
    d2 = (math.log(S / level) + (r - q - 0.5 * sigma * sigma) * T) / (sigma * sq)
    return norm_cdf(d2)


def historical_volatility(closes: list[float], window: int = 30) -> float | None:
    """Annualized standard deviation of daily log returns over the last `window` days."""
    closes = [c for c in closes if c and c > 0]
    if len(closes) < 3:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])][-window:]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)

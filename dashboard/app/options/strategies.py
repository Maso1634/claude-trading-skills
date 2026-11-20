"""Option strategies: their legs, payoff at expiry, and risk numbers.

All math here is per share first, then scaled by 100 shares per contract and
the number of contracts. A leg's `qty` is +1 for bought, -1 for sold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import pricing

SHARES_PER_CONTRACT = 100


@dataclass(frozen=True)
class Strategy:
    key: str
    name: str
    level: int  # Robinhood options level needed
    two_strikes: bool
    summary: str
    outlook: str


STRATEGIES: dict[str, Strategy] = {
    s.key: s
    for s in (
        Strategy(
            "long_call",
            "Long call",
            2,
            False,
            "Buy a call. You pay the premium and profit if the stock rises above strike + premium by expiry.",
            "Bullish. Most you can lose is what you paid.",
        ),
        Strategy(
            "long_put",
            "Long put",
            2,
            False,
            "Buy a put. You pay the premium and profit if the stock falls below strike - premium by expiry.",
            "Bearish. Most you can lose is what you paid.",
        ),
        Strategy(
            "covered_call",
            "Covered call",
            2,
            False,
            "Own 100 shares per contract and sell a call against them. You collect the premium but give up gains above the strike.",
            "Neutral to slightly bullish. You still carry the stock's downside.",
        ),
        Strategy(
            "cash_secured_put",
            "Cash-secured put",
            2,
            False,
            "Sell a put and set aside cash to buy 100 shares per contract at the strike if you're assigned.",
            "Neutral to bullish. A way to get paid while waiting to buy a stock lower.",
        ),
        Strategy(
            "bull_call_spread",
            "Bull call spread",
            3,
            True,
            "Buy a call at the lower strike and sell one at the higher strike. Cheaper than a plain call, profit is capped.",
            "Bullish with a target. Risk and reward are both fixed up front.",
        ),
        Strategy(
            "bear_put_spread",
            "Bear put spread",
            3,
            True,
            "Buy a put at the higher strike and sell one at the lower strike. Cheaper than a plain put, profit is capped.",
            "Bearish with a target. Risk and reward are both fixed up front.",
        ),
    )
}


@dataclass
class Leg:
    kind: str  # call | put | stock
    qty: int  # +1 bought, -1 sold (per contract; stock leg = 100 shares)
    strike: float | None  # None for stock
    premium: float  # per share; for stock, the cost basis
    estimated: bool = False  # premium came from Black-Scholes, not the user
    greeks: pricing.Greeks | None = None

    @property
    def label(self) -> str:
        side = "Buy" if self.qty > 0 else "Sell"
        if self.kind == "stock":
            return f"{side} 100 shares @ {self.premium:.2f}"
        return f"{side} {self.strike:g} {self.kind}"

    def value_at_expiry(self, price: float) -> float:
        if self.kind == "stock":
            return price
        return pricing.intrinsic(self.kind, price, self.strike)

    def pnl_at_expiry(self, price: float) -> float:
        return self.qty * (self.value_at_expiry(price) - self.premium)


@dataclass
class Inputs:
    strategy: str
    price: float  # current stock price
    expiry: date
    today: date
    strike: float
    strike2: float | None = None
    premium: float | None = None  # per share, leg 1; None = estimate
    premium2: float | None = None  # per share, leg 2
    iv: float = 0.30  # decimal
    rate: float = 0.04  # decimal
    contracts: int = 1
    cost_basis: float | None = None  # covered call: what you paid per share
    account: float | None = None

    @property
    def dte(self) -> int:
        return (self.expiry - self.today).days

    @property
    def years(self) -> float:
        return max(self.dte, 0) / 365


@dataclass
class Analysis:
    inputs: Inputs
    strategy: Strategy
    legs: list[Leg]
    net_premium: float  # per share; + = you paid (debit), - = you collected (credit)
    max_profit: float | None  # dollars for the whole position; None = unlimited
    max_loss: float | None  # dollars (positive number); None = unlimited
    breakevens: list[float]
    capital: float  # cash tied up to open the trade
    capital_note: str
    position_greeks: dict[str, float]
    prob_profit: float | None
    curve: list[tuple[float, float, float]] = field(
        default_factory=list
    )  # price, expiry P&L, today P&L
    warnings: list[str] = field(default_factory=list)

    @property
    def scale(self) -> int:
        return SHARES_PER_CONTRACT * self.inputs.contracts

    @property
    def risk_pct(self) -> float | None:
        if self.inputs.account and self.max_loss is not None:
            return self.max_loss / self.inputs.account * 100
        return None


def build_legs(inp: Inputs) -> list[Leg]:
    k1, k2 = inp.strike, inp.strike2
    s = inp.strategy
    if s == "long_call":
        return [Leg("call", 1, k1, inp.premium)]
    if s == "long_put":
        return [Leg("put", 1, k1, inp.premium)]
    if s == "covered_call":
        basis = inp.cost_basis if inp.cost_basis is not None else inp.price
        return [Leg("stock", 1, None, basis), Leg("call", -1, k1, inp.premium)]
    if s == "cash_secured_put":
        return [Leg("put", -1, k1, inp.premium)]
    if s == "bull_call_spread":
        return [Leg("call", 1, k1, inp.premium), Leg("call", -1, k2, inp.premium2)]
    if s == "bear_put_spread":
        return [Leg("put", 1, k1, inp.premium), Leg("put", -1, k2, inp.premium2)]
    raise ValueError(f"Unknown strategy: {s}")


def validate(inp: Inputs) -> None:
    spec = STRATEGIES.get(inp.strategy)
    if spec is None:
        raise ValueError("Pick a strategy")
    if inp.price <= 0:
        raise ValueError("Stock price must be above 0")
    if inp.strike <= 0:
        raise ValueError("Strike must be above 0")
    if inp.dte < 0:
        raise ValueError("The expiration date is in the past")
    if not 0 < inp.iv <= 5:
        raise ValueError("Implied volatility should be between 1% and 500%")
    if inp.contracts < 1 or inp.contracts > 1000:
        raise ValueError("Contracts must be between 1 and 1000")
    if spec.two_strikes:
        if not inp.strike2 or inp.strike2 <= 0:
            raise ValueError(f"A {spec.name.lower()} needs a second strike")
        if inp.strategy == "bull_call_spread" and not inp.strike2 > inp.strike:
            raise ValueError(
                "For a bull call spread the sold strike must be above the bought strike"
            )
        if inp.strategy == "bear_put_spread" and not inp.strike2 < inp.strike:
            raise ValueError(
                "For a bear put spread the sold strike must be below the bought strike"
            )
    for p in (inp.premium, inp.premium2):
        if p is not None and p < 0:
            raise ValueError("Premiums can't be negative")


def _pnl(legs: list[Leg], price: float) -> float:
    return sum(leg.pnl_at_expiry(price) for leg in legs)


def _today_pnl(legs: list[Leg], inp: Inputs, price: float) -> float:
    total = 0.0
    for leg in legs:
        if leg.kind == "stock":
            value = price
        else:
            value = pricing.greeks(leg.kind, price, leg.strike, inp.years, inp.rate, inp.iv).price
        total += leg.qty * (value - leg.premium)
    return total


def _end_slope(legs: list[Leg]) -> int:
    """P&L change per $1 when the stock is far above every strike (per share)."""
    return sum(leg.qty for leg in legs if leg.kind in ("call", "stock"))


def analyze(inp: Inputs) -> Analysis:
    validate(inp)
    spec = STRATEGIES[inp.strategy]
    legs = build_legs(inp)

    # Fill in missing premiums with the Black-Scholes estimate, and attach Greeks.
    for leg in legs:
        if leg.kind == "stock":
            leg.greeks = pricing.Greeks(inp.price, 1.0, 0.0, 0.0, 0.0, 0.0)
            continue
        g = pricing.greeks(leg.kind, inp.price, leg.strike, inp.years, inp.rate, inp.iv)
        leg.greeks = g
        if leg.premium is None:
            leg.premium = round(g.price, 2)
            leg.estimated = True

    scale = SHARES_PER_CONTRACT * inp.contracts
    option_legs = [leg for leg in legs if leg.kind != "stock"]
    net_premium = sum(leg.qty * leg.premium for leg in option_legs)

    # Payoff is straight lines between strikes, so the extremes are at 0, a strike,
    # or out at infinity (decided by the slope past the highest strike).
    strikes = sorted({leg.strike for leg in option_legs})
    points = [0.0] + strikes
    values = [_pnl(legs, p) for p in points]
    slope = _end_slope(legs)
    max_profit = None if slope > 0 else max(values) * scale
    max_loss = None if slope < 0 else max(0.0, -min(values)) * scale

    # Breakevens: where the expiry P&L crosses zero.
    breakevens: list[float] = []
    for (a, va), (b, vb) in zip(zip(points, values), zip(points[1:], values[1:])):
        if va == 0 and a > 0:
            breakevens.append(a)
        if (va < 0 < vb) or (va > 0 > vb):
            breakevens.append(a + (b - a) * (-va) / (vb - va))
    if values[-1] == 0 and points[-1] > 0 and points[-1] not in breakevens:
        breakevens.append(points[-1])
    if slope != 0 and (values[-1] < 0 < slope or values[-1] > 0 > slope):
        breakevens.append(points[-1] + (-values[-1]) / slope)
    breakevens = sorted({round(b, 2) for b in breakevens})

    # Chance the expiry P&L is above zero, from the same model (rough).
    prob = _prob_profit(legs, inp, breakevens)

    # Cash needed to open the trade.
    if inp.strategy == "cash_secured_put":
        capital = inp.strike * scale
        capital_note = (
            f"Robinhood holds the strike x 100 per contract as collateral. "
            f"The {abs(net_premium) * scale:,.2f} premium you collect lands in your account."
        )
    elif inp.strategy == "covered_call":
        capital = legs[0].premium * scale
        capital_note = (
            "The 100 shares per contract you must own. The call premium is extra cash on top."
        )
    else:
        capital = max(net_premium, 0.0) * scale
        capital_note = "The premium you pay up front (the debit). It's also your maximum loss."

    pg = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for leg in legs:
        for k in pg:
            pg[k] += leg.qty * getattr(leg.greeks, k) * scale

    lo = max(0.01, min([inp.price * 0.6] + [k * 0.85 for k in strikes]))
    hi = max([inp.price * 1.4] + [k * 1.15 for k in strikes])
    curve = []
    steps = 60
    for i in range(steps + 1):
        p = lo + (hi - lo) * i / steps
        curve.append((p, _pnl(legs, p) * scale, _today_pnl(legs, inp, p) * scale))

    a = Analysis(
        inputs=inp,
        strategy=spec,
        legs=legs,
        net_premium=net_premium,
        max_profit=max_profit,
        max_loss=max_loss,
        breakevens=breakevens,
        capital=capital,
        capital_note=capital_note,
        position_greeks=pg,
        prob_profit=prob,
        curve=curve,
    )
    a.warnings = _warnings(a)
    return a


def _prob_profit(legs: list[Leg], inp: Inputs, breakevens: list[float]) -> float | None:
    edges = [0.0] + breakevens + [float("inf")]
    total = 0.0
    for lo, hi in zip(edges, edges[1:]):
        mid = (lo + hi) / 2 if hi != float("inf") else (lo or inp.price) * 2 + 1
        if _pnl(legs, mid) <= 0:
            continue
        p_lo = pricing.prob_above(inp.price, lo, inp.years, inp.rate, inp.iv)
        p_hi = (
            0.0
            if hi == float("inf")
            else pricing.prob_above(inp.price, hi, inp.years, inp.rate, inp.iv)
        )
        total += p_lo - p_hi
    return max(0.0, min(1.0, total))


def _warnings(a: Analysis) -> list[str]:
    out = []
    inp = a.inputs
    if a.strategy.level > 2:
        out.append(
            f"A {a.strategy.name.lower()} needs options Level 3 on Robinhood. "
            "You're on Level 2, so this is for learning until you're approved."
        )
    if inp.dte <= 7:
        out.append(
            "7 days or less to expiration: time decay (theta) is fastest now and small stock moves swing the price a lot."
        )
    if a.risk_pct is not None and a.risk_pct > 2:
        out.append(
            f"Max loss is {a.risk_pct:.1f}% of your account. A common rule is to risk 1-2% per trade."
        )
    if a.max_loss is None:
        out.append("The loss on this position has no fixed limit.")
    if inp.account and a.capital > inp.account:
        out.append("This trade needs more cash than your account size.")
    if any(leg.estimated for leg in a.legs):
        out.append(
            "Some premiums are Black-Scholes estimates, not market prices. Type the real price from Robinhood for accurate numbers."
        )
    return out

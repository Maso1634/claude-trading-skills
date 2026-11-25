"""/options: payoff and risk calculator. Read-only: it saves nothing and places no orders.

With IBKR on (IBKR_HOST in .env), a ticker brings in the real option chain and each
leg's market bid/ask, implied volatility and Greeks from IB Gateway. Alpaca is the
fallback for the stock price, and a cross-check when both are available.
"""

from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, render_template, request

from ..alpaca import AlpacaError
from ..charts import payoff_chart
from ..ibkr import IbkrError
from . import pricing
from .strategies import STRATEGIES, Inputs, analyze

bp = Blueprint("options", __name__, url_prefix="/options")

# One line of plain English per number, shown under it on the page.
EXPLAIN = {
    "delta": "How many dollars the position gains (or loses, if negative) when the stock rises $1. "
    "It's also a rough stand-in for share exposure: delta 40 acts like owning 40 shares.",
    "gamma": "How much delta changes per $1 stock move. High gamma means your exposure shifts quickly, "
    "most of all near the strike close to expiration.",
    "theta": "Dollars the position gains or loses per calendar day from time passing alone. "
    "Negative means time works against you (buyers); positive means it pays you (sellers).",
    "vega": "Dollars gained or lost when implied volatility moves 1 percentage point. "
    "Buyers want volatility to rise; sellers want it to fall.",
    "iv": "Implied volatility: the yearly move the option's price implies, as a percent. "
    "Robinhood shows it on each contract's detail page. It's the input that matters most here.",
    "hv": "Historical volatility: how much the stock actually moved over the last 30 trading days, yearly. "
    "A starting guess for IV, not a replacement for it.",
    "breakeven": "The stock price at expiration where the trade makes exactly $0.",
    "prob": "The model's chance the trade is profitable at expiration. It assumes IV is right and "
    "moves are random, so treat it as a rough guide.",
    "mid": "Mid: halfway between the bid (what buyers offer) and the ask (what sellers want). "
    "A limit order near the mid usually fills; a market order pays the ask. A wide gap between bid and ask "
    "means the option trades thinly and costs more to get in and out of.",
    "market_vs_model": "The model price is Black-Scholes with the IV above. The market price is what people "
    "are actually paying. When they differ, the market is usually right: it knows about earnings dates, "
    "dividends and demand that the formula doesn't.",
    "rate": "The risk-free interest rate (about the 3-month Treasury bill). It barely changes the answer for short trades.",
}


def _tz() -> ZoneInfo:
    return ZoneInfo(current_app.config.get("TIMEZONE", "America/New_York"))


def _num(name: str, values: dict[str, Any] | None = None) -> float | None:
    source = request.args if values is None else values
    raw = str(source.get(name) or "").strip().replace("$", "").replace(",", "")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"'{raw}' isn't a number ({name.replace('_', ' ')})") from None
    if not math.isfinite(value):
        raise ValueError(f"'{raw}' isn't a number")
    return value


def _env_account() -> float | None:
    try:
        value = float(os.environ.get("CB_ACCOUNT_SIZE", "") or 0)
    except ValueError:
        return None
    return value or None


# Which option each leg is, and which form field holds its strike.
LEG_RIGHTS = {
    "long_call": [("C", "strike")],
    "long_put": [("P", "strike")],
    "covered_call": [("C", "strike")],
    "cash_secured_put": [("P", "strike")],
    "bull_call_spread": [("C", "strike"), ("C", "strike2")],
    "bear_put_spread": [("P", "strike"), ("P", "strike2")],
}
MISMATCH_PCT = 1.0


def _first_listed(expirations: list[date], today: date, min_days: int = 28) -> date | None:
    later = [e for e in expirations if (e - today).days >= min_days]
    return later[0] if later else (expirations[-1] if expirations else None)


def _nearest(strikes: list[float], k: float, n: int = 2) -> list[float]:
    return sorted(sorted(strikes, key=lambda s: abs(s - k))[:n])


def _ibkr_lookup(ib, symbol: str, form: dict[str, Any], today: date) -> dict[str, Any]:
    """Stock quote, option chain and each leg's market quote, over one gateway connection."""
    out: dict[str, Any] = {"quote": None, "chain": None, "legs": [], "notes": [], "error": None}
    try:
        with ib.session() as s:
            out["quote"] = s.stock_quote(symbol)
            try:
                chain = s.option_chain(symbol)
            except IbkrError as exc:
                out["notes"].append(str(exc))
                return out
            out["chain"] = chain
            if not form.get("expiry_set"):
                first = _first_listed(chain["expirations"], today)
                if first:
                    form["expiry"] = first.isoformat()
            try:
                expiry = date.fromisoformat(form.get("expiry", ""))
            except ValueError:
                return out
            if expiry not in chain["expirations"]:
                out["notes"].append(
                    f"No {symbol} options expire on {expiry:%b %d, %Y}. Pick a listed date to see market prices."
                )
                return out
            for right, field in LEG_RIGHTS.get(form.get("strategy", ""), []):
                try:
                    k = _num(field)
                except ValueError:
                    k = None
                if k is None:
                    continue
                if k not in chain["strikes"]:
                    near = " or ".join(f"{x:g}" for x in _nearest(chain["strikes"], k))
                    out["notes"].append(f"{k:g} isn't a listed strike. Nearest: {near}.")
                    continue
                q = s.option_quote(symbol, expiry, k, right)
                out["legs"].append({"field": field, "right": right, "strike": k, **q})
    except IbkrError as exc:
        out["error"] = str(exc)
    except ValueError as exc:
        out["error"] = str(exc)
    return out


def _next_friday(today: date, min_days: int = 28) -> date:
    d = today + timedelta(days=min_days)
    return d + timedelta(days=(4 - d.weekday()) % 7)


@bp.get("/")
def calculator():
    today = datetime.now(_tz()).date()
    args = request.args
    form: dict[str, Any] = {k: args.get(k, "") for k in args}
    form.setdefault("strategy", "long_call")
    form.setdefault("contracts", "1")
    form.setdefault("rate", "4")
    form["expiry_set"] = bool(args.get("expiry"))
    form.setdefault("expiry", _next_friday(today).isoformat())
    if not form.get("account"):
        acct = _env_account()
        form["account"] = f"{acct:g}" if acct else ""

    error = None
    quote = None
    quote_error = None
    hv = None
    analysis = None
    chart = None

    symbol = (args.get("symbol") or "").strip().upper()
    form["symbol"] = symbol
    client = current_app.config.get("ALPACA")
    ib = current_app.config.get("IBKR")
    market: dict[str, Any] | None = None
    cross_check = None

    if symbol and ib is not None:
        market = _ibkr_lookup(ib, symbol, form, today)
        quote = market["quote"]
        if quote:
            hv = pricing.historical_volatility(quote["closes"])

    if symbol and quote is None and client is not None:
        try:
            quote = client.stock_quote(symbol)
            quote["source"] = "Alpaca IEX"
            hv = pricing.historical_volatility(quote["closes"])
        except AlpacaError as exc:
            quote_error = str(exc)
    elif symbol and quote and client is not None:
        try:
            other = client.stock_quote(symbol).get("price")
        except AlpacaError:
            other = None
        if other and quote.get("price"):
            gap = abs(quote["price"] - other) / other * 100
            cross_check = {"price": other, "gap": gap, "mismatch": gap > MISMATCH_PCT}
    elif symbol and quote is None:
        quote_error = (
            "No price source is set up (IBKR or Alpaca), so type the stock price yourself."
        )
        if market and market["error"]:
            quote_error = f"IBKR: {market['error']}. Type the stock price yourself."

    if not form.get("price") and quote and quote.get("price"):
        form["price"] = f"{quote['price']:.2f}"

    legs = (market or {}).get("legs") or []
    by_field = {leg["field"]: leg for leg in legs}
    for field, prem in (("strike", "premium"), ("strike2", "premium2")):
        leg = by_field.get(field)
        if leg and not form.get(prem) and leg.get("mid") is not None:
            form[prem] = f"{leg['mid']:.2f}"
            form[f"{prem}_from_market"] = True
    first_leg = by_field.get("strike")
    if not form.get("iv") and first_leg and first_leg.get("iv"):
        form["iv"] = f"{first_leg['iv'] * 100:.1f}"
        form["iv_from_market"] = True
    elif not form.get("iv") and hv:
        form["iv"] = f"{hv * 100:.0f}"
        form["iv_from_hv"] = True

    submitted = bool(args.get("price") or args.get("strike") or args.get("go"))
    if submitted or (form.get("price") and form.get("strike")):
        try:
            price = _num("price") if args.get("price") else (quote or {}).get("price")
            strike = _num("strike")
            if price is None:
                raise ValueError("Enter the stock price (or a ticker so it can be looked up)")
            if strike is None:
                raise ValueError("Enter a strike price")
            try:
                expiry = date.fromisoformat(form["expiry"])
            except ValueError:
                raise ValueError("Pick an expiration date") from None
            iv_pct = _num("iv", form) if form.get("iv") else None
            if iv_pct is None:
                raise ValueError("Enter implied volatility (IV) as a percent, e.g. 35")
            rate_pct = _num("rate")
            contracts = _num("contracts") or 1
            if contracts != int(contracts):
                raise ValueError("Contracts must be a whole number")
            inp = Inputs(
                strategy=form["strategy"],
                price=price,
                expiry=expiry,
                today=today,
                strike=strike,
                strike2=_num("strike2"),
                premium=_num("premium", form),
                premium2=_num("premium2", form),
                iv=iv_pct / 100,
                rate=(rate_pct if rate_pct is not None else 4) / 100,
                contracts=int(contracts),
                cost_basis=_num("cost_basis"),
                account=_num("account"),
            )
            analysis = analyze(inp)
            chart = payoff_chart(analysis.curve, inp.price, analysis.breakevens)
        except ValueError as exc:
            error = str(exc)

    return render_template(
        "options.html",
        form=form,
        strategies=STRATEGIES,
        a=analysis,
        chart=chart,
        quote=quote,
        quote_error=quote_error,
        hv=hv,
        explain=EXPLAIN,
        error=error,
        alpaca_ready=client is not None,
        ibkr_ready=ib is not None,
        market=market,
        cross_check=cross_check,
        model_legs={
            leg.strike: leg for leg in (analysis.legs if analysis else []) if leg.kind != "stock"
        },
        strike_choices=_strike_choices(market, quote),
        expiry_choices=_expiry_choices(market, today, form.get("expiry")),
    )


def _strike_choices(market, quote) -> list[float]:
    chain = (market or {}).get("chain")
    price = (quote or {}).get("price")
    if not chain:
        return []
    if not price:
        return chain["strikes"][:200]
    return [k for k in chain["strikes"] if 0.75 * price <= k <= 1.25 * price]


def _expiry_choices(market, today: date, current: str | None) -> list[str]:
    chain = (market or {}).get("chain")
    if not chain:
        return []
    days = [e.isoformat() for e in chain["expirations"] if 0 <= (e - today).days <= 400][:30]
    if current and current not in days:
        days.insert(0, current)
    return days

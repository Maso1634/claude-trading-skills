"""/options: payoff and risk calculator. Read-only: it saves nothing and places no orders."""

from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, render_template, request

from ..alpaca import AlpacaError
from ..charts import payoff_chart
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
    "rate": "The risk-free interest rate (about the 3-month Treasury bill). It barely changes the answer for short trades.",
}


def _tz() -> ZoneInfo:
    return ZoneInfo(current_app.config.get("TIMEZONE", "America/New_York"))


def _num(name: str) -> float | None:
    raw = (request.args.get(name) or "").strip().replace("$", "").replace(",", "")
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
    if symbol and client is not None:
        try:
            quote = client.stock_quote(symbol)
            hv = pricing.historical_volatility(quote["closes"])
        except AlpacaError as exc:
            quote_error = str(exc)
    elif symbol:
        quote_error = "Alpaca keys aren't set, so type the stock price yourself."

    if not form.get("price") and quote and quote.get("price"):
        form["price"] = f"{quote['price']:.2f}"
    if not form.get("iv") and hv:
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
            iv_pct = _num("iv") if args.get("iv") else (hv * 100 if hv else None)
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
                premium=_num("premium"),
                premium2=_num("premium2"),
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
    )

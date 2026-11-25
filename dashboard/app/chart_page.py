"""/chart: a price chart for one ticker from IBKR (intraday or daily). Read-only."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, render_template, request

from .alpaca import AlpacaError
from .charts import line_chart
from .ibkr import IbkrError

bp = Blueprint("chart", __name__, url_prefix="/chart")

# key: (label, IBKR duration, IBKR bar size, how to label a bar)
RANGES: dict[str, tuple[str, str, str, str]] = {
    "1d": ("1 day · 5 min", "1 D", "5 mins", "%H:%M"),
    "5d": ("5 days · 30 min", "5 D", "30 mins", "%a %H:%M"),
    "3m": ("3 months · daily", "3 M", "1 day", "%b %d"),
    "1y": ("1 year · weekly", "1 Y", "1 week", "%b %d '%y"),
}
MISMATCH_PCT = 1.0


def _label(t: Any, fmt: str) -> str:
    return t.strftime(fmt) if hasattr(t, "strftime") else str(t)


def summarize(bars: list[dict[str, Any]], prev_close: float | None) -> dict[str, Any]:
    last = bars[-1]["close"]
    base = prev_close or bars[0]["close"]
    highs = [b["high"] for b in bars if b.get("high")]
    lows = [b["low"] for b in bars if b.get("low")]
    vols = [b["volume"] for b in bars if b.get("volume")]
    return {
        "last": last,
        "change": last - base,
        "change_pct": (last - base) / base * 100 if base else None,
        "high": max(highs) if highs else None,
        "low": min(lows) if lows else None,
        "volume": sum(vols) if vols else None,
        "base_label": "yesterday's close" if prev_close else "start of the range",
    }


@bp.get("/")
def page():
    symbol = (request.args.get("symbol") or "").strip().upper()
    rng = request.args.get("range", "1d")
    if rng not in RANGES:
        rng = "1d"
    label, duration, size, fmt = RANGES[rng]
    ib = current_app.config.get("IBKR")
    alpaca_client = current_app.config.get("ALPACA")
    ctx: dict[str, Any] = {
        "symbol": symbol,
        "range": rng,
        "ranges": RANGES,
        "ibkr_ready": ib is not None,
        "error": None,
        "chart": None,
        "summary": None,
        "quote": None,
        "cross_check": None,
        "range_label": label,
    }
    if symbol and ib is None:
        ctx["error"] = "IBKR is off (IBKR_HOST isn't set in .env), so there's no chart data."
    elif symbol:
        try:
            with ib.session() as s:
                quote = s.stock_quote(symbol)
                bars = s.bars(symbol, duration, size)
        except (IbkrError, ValueError) as exc:
            ctx["error"] = str(exc)
        else:
            if not bars:
                ctx["error"] = f"IBKR returned no bars for {symbol}."
            else:
                ctx["quote"] = quote
                prev = quote.get("prev_close") if rng == "1d" else None
                summary = summarize(bars, prev)
                ctx["summary"] = summary
                closes = [b["close"] for b in bars]
                lo, hi = min(closes + ([prev] if prev else [])), max(closes)
                pad = (hi - lo) * 0.08 or hi * 0.01
                ctx["chart"] = line_chart(
                    [(_label(b["t"], fmt), round(b["close"], 2)) for b in bars],
                    y_min=round(lo - pad, 2),
                    y_max=round(hi + pad, 2),
                    width=760,
                    height=280,
                    label=f"{symbol} {label}",
                    pad_left=58,
                    extra_class="dense",
                )
                if alpaca_client is not None:
                    try:
                        other = alpaca_client.stock_quote(symbol).get("price")
                    except AlpacaError:
                        other = None
                    if other:
                        gap = abs(summary["last"] - other) / other * 100
                        ctx["cross_check"] = {
                            "price": other,
                            "gap": gap,
                            "mismatch": gap > MISMATCH_PCT,
                        }
    return render_template("chart.html", **ctx)

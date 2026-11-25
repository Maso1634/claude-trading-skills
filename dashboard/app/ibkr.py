"""Read-only Interactive Brokers (IBKR) client for the dashboard.

Talks to IB Gateway (the `ib-gateway` service in compose.yaml) over its socket API.
It connects with readonly=True and the gateway itself runs with READ_ONLY_API=yes,
so there is no code path that places, changes or cancels an order.

Phase 1 only checks the connection and fetches a quote:
    docker compose exec dashboard python -m app.ibkr SPY
"""

from __future__ import annotations

import math
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
PAPER_PORT = 4004  # gnzsnz/ib-gateway forwards the paper API here
LIVE_PORT = 4003
# reqMarketDataType: 1 = live, 3 = delayed (free, about 15 minutes behind)
DATA_TYPES = {"live": 1, "delayed": 3}
DATA_NAMES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}
CLIENT_ID = 17  # any number not used by another API client on the same gateway


@dataclass(frozen=True)
class IbkrSettings:
    host: str
    port: int
    paper: bool
    market_data_type: int
    timeout: int = 10


def settings_from_env(env: Mapping[str, str]) -> IbkrSettings | None:
    host = (env.get("IBKR_HOST") or "").strip()
    if not host:
        return None
    paper = (env.get("IBKR_MODE") or "").strip().lower() != "live"
    data = (env.get("IBKR_MARKET_DATA") or "").strip().lower()
    return IbkrSettings(
        host=host,
        port=PAPER_PORT if paper else LIVE_PORT,
        paper=paper,
        market_data_type=DATA_TYPES.get(data, DATA_TYPES["delayed"]),
    )


def _clean(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _default_ib():
    try:
        from ib_async import IB
    except ImportError:  # pragma: no cover
        print("ib_async is missing: pip install -r requirements.txt", file=sys.stderr)
        raise SystemExit(2) from None
    return IB()


def fetch_quote(
    settings: IbkrSettings, symbol: str, ib_factory: Callable[[], Any] | None = None
) -> dict[str, Any]:
    symbol = symbol.strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise ValueError(f"not a stock symbol: {symbol!r}")
    from types import SimpleNamespace

    try:
        from ib_async import Stock

        contract = Stock(symbol, "SMART", "USD")
    except ImportError:
        contract = SimpleNamespace(symbol=symbol, exchange="SMART", currency="USD")

    ib = (ib_factory or _default_ib)()
    ib.connect(
        settings.host,
        settings.port,
        clientId=CLIENT_ID,
        readonly=True,
        timeout=settings.timeout,
    )
    try:
        ib.reqMarketDataType(settings.market_data_type)
        (contract,) = ib.qualifyContracts(contract)
        ticker = ib.reqMktData(contract, snapshot=True)
        ib.sleep(3)
        kind = getattr(ticker, "marketDataType", settings.market_data_type)
        return {
            "symbol": symbol,
            "last": _clean(ticker.last),
            "bid": _clean(ticker.bid),
            "ask": _clean(ticker.ask),
            "close": _clean(ticker.close),
            "data": DATA_NAMES.get(kind, str(kind)),
            "account": "paper" if settings.paper else "live",
        }
    finally:
        ib.disconnect()


def main(argv: list[str]) -> int:
    settings = settings_from_env(os.environ)
    if settings is None:
        print("IBKR_HOST is not set in .env, so IBKR is off.", file=sys.stderr)
        return 1
    symbol = argv[1] if len(argv) > 1 else "SPY"
    try:
        quote = fetch_quote(settings, symbol)
    except (ConnectionError, OSError, TimeoutError) as exc:
        print(
            f"Could not reach IB Gateway at {settings.host}:{settings.port}: {exc}", file=sys.stderr
        )
        print("Check: docker compose logs ib-gateway (is it logged in?)", file=sys.stderr)
        return 1
    for k, v in quote.items():
        print(f"{k:>8}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))


# ---------------------------------------------------------------------------
# Reader used by the web pages (phase 2). Still read-only.
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")
RIGHTS = {"C", "P"}


class IbkrError(RuntimeError):
    pass


def _price_from(ticker: Any) -> float | None:
    """Last trade if there is one, else the bid/ask midpoint, else the previous close."""
    last = _clean(getattr(ticker, "last", None))
    if last and last > 0:
        return last
    bid, ask = _clean(getattr(ticker, "bid", None)), _clean(getattr(ticker, "ask", None))
    if bid and ask and bid > 0 and ask > 0:
        return round((bid + ask) / 2, 4)
    close = _clean(getattr(ticker, "close", None))
    return close if close and close > 0 else None


def completed_daily_closes(bars: list[Any], now: datetime) -> list[float]:
    """Daily closes without today's bar while the session is still open.

    IBKR's daily bars include today's bar as it forms. A screener or volatility
    estimate that counted it would treat half a day as a full one.
    """
    now_et = now.astimezone(ET)
    session_over = (now_et.hour, now_et.minute) >= (16, 0)
    out = []
    for b in bars:
        d = b.date if isinstance(b.date, date) else None
        if isinstance(b.date, datetime):
            d = b.date.astimezone(ET).date()
        if d == now_et.date() and not session_over:
            continue
        c = _clean(b.close)
        if c and c > 0:
            out.append(c)
    return out


class IbkrSession:
    """One gateway connection, opened on the first cache miss and closed at the end."""

    def __init__(self, reader: IbkrReader) -> None:
        self._r = reader
        self._ib = None

    def __enter__(self) -> IbkrSession:
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._ib is not None:
            try:
                self._ib.disconnect()
            finally:
                self._ib = None

    def _conn(self):
        if self._ib is None:
            s = self._r.settings
            ib = self._r.ib_factory()
            try:
                ib.connect(
                    s.host,
                    s.port,
                    clientId=100 + os.getpid() % 800,  # one per gunicorn worker
                    readonly=True,
                    timeout=s.timeout,
                )
            except (ConnectionError, OSError, TimeoutError) as exc:
                raise IbkrError(
                    "IB Gateway isn't answering (still logging in, or waiting for 2FA)"
                ) from exc
            ib.reqMarketDataType(s.market_data_type)
            self._ib = ib
        return self._ib

    def _wait(self, ticker: Any, need_greeks: bool, seconds: float) -> None:
        ib = self._ib
        steps = max(1, int(seconds / 0.5))
        for _ in range(steps):
            ib.sleep(0.5)
            has_quote = _price_from(ticker) is not None
            if has_quote and (not need_greeks or getattr(ticker, "modelGreeks", None)):
                return

    def stock_quote(self, symbol: str) -> dict[str, Any]:
        symbol = _check_symbol(symbol)
        return self._r.cached(("stock", symbol), 60, lambda: self._stock_quote(symbol))

    def _stock_quote(self, symbol: str) -> dict[str, Any]:
        from ib_async import Stock

        ib = self._conn()
        found = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
        contract = next((c for c in found or [] if c is not None), None)
        if contract is None:
            raise IbkrError(f"IBKR doesn't know the stock {symbol}")
        ticker = ib.reqMktData(contract)
        self._wait(ticker, need_greeks=False, seconds=4)
        ib.cancelMktData(contract)
        bars = ib.reqHistoricalData(contract, "", "4 M", "1 day", "TRADES", True)
        kind = getattr(ticker, "marketDataType", self._r.settings.market_data_type)
        return {
            "symbol": symbol,
            "price": _price_from(ticker),
            "bid": _clean(ticker.bid),
            "ask": _clean(ticker.ask),
            "prev_close": _clean(ticker.close),
            "closes": completed_daily_closes(list(bars or []), self._r.now()),
            "data": DATA_NAMES.get(kind, str(kind)),
            "con_id": contract.conId,
            "source": "IBKR",
        }

    def bars(self, symbol: str, duration: str, size: str) -> list[dict[str, Any]]:
        """Price bars, oldest first. Intraday bars are regular hours only, timestamps in ET.

        The newest bar can still be forming (today's daily bar, or the current 5 minutes).
        """
        symbol = _check_symbol(symbol)
        ttl = 60 if "min" in size or "hour" in size else 900
        return self._r.cached(
            ("bars", symbol, duration, size), ttl, lambda: self._bars(symbol, duration, size)
        )

    def _bars(self, symbol: str, duration: str, size: str) -> list[dict[str, Any]]:
        con_id = self.stock_quote(symbol)["con_id"]
        from ib_async import Stock

        contract = Stock(symbol, "SMART", "USD")
        contract.conId = con_id
        raw = self._conn().reqHistoricalData(contract, "", duration, size, "TRADES", True) or []
        out = []
        for b in raw:
            t = b.date
            if isinstance(t, datetime):
                t = t.astimezone(ET)
            close = _clean(b.close)
            if close is None or close <= 0:
                continue
            out.append(
                {
                    "t": t,
                    "open": _clean(getattr(b, "open", None)),
                    "high": _clean(getattr(b, "high", None)),
                    "low": _clean(getattr(b, "low", None)),
                    "close": close,
                    "volume": _clean(getattr(b, "volume", None)),
                }
            )
        return out

    def option_chain(self, symbol: str) -> dict[str, Any]:
        symbol = _check_symbol(symbol)
        return self._r.cached(("chain", symbol), 900, lambda: self._option_chain(symbol))

    def _option_chain(self, symbol: str) -> dict[str, Any]:
        con_id = self.stock_quote(symbol)["con_id"]
        params = self._conn().reqSecDefOptParams(symbol, "", "STK", con_id) or []
        smart = [p for p in params if p.exchange == "SMART"] or list(params)
        if not smart:
            raise IbkrError(f"IBKR lists no options for {symbol}")
        expirations = sorted(
            {date(int(e[:4]), int(e[4:6]), int(e[6:8])) for p in smart for e in p.expirations}
        )
        strikes = sorted({float(k) for p in smart for k in p.strikes})
        return {"expirations": expirations, "strikes": strikes}

    def option_quote(self, symbol: str, expiry: date, strike: float, right: str) -> dict:
        symbol = _check_symbol(symbol)
        right = right.upper()[:1]
        if right not in RIGHTS:
            raise ValueError("right must be C or P")
        key = ("option", symbol, expiry.isoformat(), float(strike), right)
        return self._r.cached(key, 30, lambda: self._option_quote(symbol, expiry, strike, right))

    def _option_quote(self, symbol: str, expiry: date, strike: float, right: str) -> dict:
        from ib_async import Option

        ib = self._conn()
        opt = Option(
            symbol, expiry.strftime("%Y%m%d"), float(strike), right, "SMART", currency="USD"
        )
        found = ib.qualifyContracts(opt)
        contract = next((c for c in found or [] if c is not None), None)
        if contract is None:
            raise IbkrError(f"IBKR has no {symbol} {expiry} {strike:g} {right} contract")
        ticker = ib.reqMktData(contract)
        self._wait(ticker, need_greeks=True, seconds=6)
        ib.cancelMktData(contract)
        bid, ask = _clean(ticker.bid), _clean(ticker.ask)
        bid = bid if bid is not None and bid >= 0 else None
        ask = ask if ask is not None and ask > 0 else None
        g = getattr(ticker, "modelGreeks", None)
        kind = getattr(ticker, "marketDataType", self._r.settings.market_data_type)

        def greek(name: str) -> float | None:
            return _clean(getattr(g, name, None)) if g else None

        return {
            "bid": bid,
            "ask": ask,
            "mid": round((bid + ask) / 2, 4) if bid is not None and ask is not None else None,
            "last": _clean(ticker.last),
            "iv": greek("impliedVol"),
            "delta": greek("delta"),
            "gamma": greek("gamma"),
            "theta": greek("theta"),
            "vega": greek("vega"),
            "data": DATA_NAMES.get(kind, str(kind)),
        }


class IbkrReader:
    """Read-only quotes, daily bars and option data from IB Gateway, with small caches.

    Caches keep page reloads fast and stay well under IBKR's request pacing limits.
    """

    def __init__(
        self,
        settings: IbkrSettings,
        ib_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.ib_factory = ib_factory or _default_ib
        self.now = clock or (lambda: datetime.now(ET))
        self._cache: dict[tuple, tuple[float, Any]] = {}

    @property
    def paper(self) -> bool:
        return self.settings.paper

    @property
    def data_label(self) -> str:
        return DATA_NAMES.get(self.settings.market_data_type, "delayed")

    def cached(self, key: tuple, ttl: float, fn: Callable[[], Any]) -> Any:
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = fn()
        self._cache[key] = (now, value)
        if len(self._cache) > 500:
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:100]
            for k, _ in oldest:
                self._cache.pop(k, None)
        return value

    def session(self) -> IbkrSession:
        return IbkrSession(self)


def _check_symbol(symbol: str) -> str:
    symbol = (symbol or "").strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise ValueError(f"not a stock symbol: {symbol!r}")
    return symbol


def reader_from_env(env: Mapping[str, str]) -> IbkrReader | None:
    settings = settings_from_env(env)
    return IbkrReader(settings) if settings else None

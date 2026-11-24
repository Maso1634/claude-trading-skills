"""Read-only Interactive Brokers (IBKR) client for the dashboard.

Talks to IB Gateway (the `ib-gateway` service in compose.yaml) over its socket API.
It connects with readonly=True and the gateway itself runs with READ_ONLY_API=yes,
so there is no code path that places, changes or cancels an order.

Phase 1 only checks the connection and fetches a quote:
    docker compose exec dashboard python -m app.ibkr SPY
"""

from __future__ import annotations

import math
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

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
    import os

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

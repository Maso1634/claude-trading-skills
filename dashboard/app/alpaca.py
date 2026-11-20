"""A deliberately tiny, read-only Alpaca client.

It only sends GET requests: the account and open positions (trading API), plus a
stock's latest trade and daily bars (market data API) for the Options page.
There is no code path that places, changes or cancels an order.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class AlpacaError(RuntimeError):
    pass


class AlpacaReadOnly:
    def __init__(
        self,
        key_id: str,
        secret: str,
        paper: bool = True,
        timeout: int = 10,
        cache_seconds: int = 60,
    ) -> None:
        self._key_id = key_id
        self._secret = secret
        self.paper = paper
        self.base_url = PAPER_URL if paper else LIVE_URL
        self._timeout = timeout
        self._cache_seconds = cache_seconds
        self._cached: tuple[float, dict[str, Any]] | None = None
        self._quotes: dict[str, tuple[float, dict[str, Any]]] = {}

    def _get(self, path: str, base: str | None = None) -> Any:
        req = Request(
            (base or self.base_url) + path,
            method="GET",
            headers={
                "APCA-API-KEY-ID": self._key_id,
                "APCA-API-SECRET-KEY": self._secret,
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read())
        except Exception as exc:  # network, HTTP 4xx/5xx, bad JSON
            # Never echo headers: they hold the keys.
            where = path.split("?")[0]
            raise AlpacaError(f"Alpaca request to {where} failed: {type(exc).__name__}") from None

    def account(self) -> dict[str, Any]:
        return self._get("/v2/account")

    def positions(self) -> list[dict[str, Any]]:
        return self._get("/v2/positions")

    def snapshot(self) -> dict[str, Any]:
        """Account + positions, cached briefly so page reloads don't hammer the API."""
        now = time.monotonic()
        if self._cached and now - self._cached[0] < self._cache_seconds:
            return self._cached[1]
        try:
            snap = {"account": self.account(), "positions": self.positions(), "error": None}
        except AlpacaError as exc:
            snap = {"account": None, "positions": [], "error": str(exc)}
        snap["paper"] = self.paper
        self._cached = (now, snap)
        return snap

    def stock_quote(self, symbol: str) -> dict[str, Any]:
        """Latest price and about 3 months of daily closes for one stock, cached for a minute.

        Latest price: the last IEX trade (IEX is one exchange, free and real time;
        the full-market SIP feed needs a paid plan for the last 15 minutes).
        Closes: SIP daily bars ending 16 minutes ago, which the free plan allows.
        """
        symbol = symbol.strip().upper()
        if not SYMBOL_RE.match(symbol):
            raise AlpacaError("That doesn't look like a ticker symbol")
        now = time.monotonic()
        hit = self._quotes.get(symbol)
        if hit and now - hit[0] < self._cache_seconds:
            return hit[1]
        trade = self._get(f"/v2/stocks/{symbol}/trades/latest?feed=iex", DATA_URL)
        price = float((trade.get("trade") or {}).get("p") or 0) or None
        end = datetime.now(UTC) - timedelta(minutes=16)
        query = urlencode(
            {
                "timeframe": "1Day",
                "start": (end - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "feed": "sip",
                "adjustment": "all",
                "limit": 1000,
            }
        )
        bars = self._get(f"/v2/stocks/{symbol}/bars?{query}", DATA_URL).get("bars") or []
        closes = [float(b["c"]) for b in bars if b.get("c")]
        quote = {
            "symbol": symbol,
            "price": price,
            "price_time": (trade.get("trade") or {}).get("t"),
            "closes": closes,
        }
        self._quotes[symbol] = (now, quote)
        return quote


def from_env(env: Mapping[str, str]) -> AlpacaReadOnly | None:
    key = env.get("ALPACA_API_KEY", "").strip()
    secret = env.get("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        return None
    return AlpacaReadOnly(key, secret, paper=is_paper(env))


def is_paper(env: Mapping[str, str]) -> bool:
    """ALPACA_ENV=paper|live decides. Anything other than an explicit "live" stays paper.

    ALPACA_PAPER=false (the repo's older convention) is honored only when ALPACA_ENV is unset.
    """
    mode = env.get("ALPACA_ENV", "").strip().lower()
    if mode:
        return mode != "live"
    return env.get("ALPACA_PAPER", "true").strip().lower() not in {"false", "0", "no"}

"""Read Interactive Brokers (IBKR) trades into journal fills, from a Flex Query.

A Flex Query is a report you define once in IBKR's Client Portal
(Performance & Reports > Flex Queries > Activity Flex Query). Set it up with the
Trades section at Execution level, XML format, dates yyyyMMdd, times HHmmss,
separator ';'. The journal gets it in one of two ways:

- Fetch: the Flex Web Service hands the report over in exchange for a token
  (Settings > Account Settings > Reporting > Flex Web Service) and the query ID.
  Both go in the dashboard's .env as IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID.
  The token can only read reports, never trade.
- Upload: run the query in Client Portal, download the XML, upload it.

IBKR gives each execution a tradeID, so importing the same period twice is safe.
Times are US Eastern (IBKR's default for US accounts) and stored as UTC.
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import db
from .pnl import ZERO, dec
from .robinhood import ParsedFill, ParseResult

SEND_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
ASSETS = {"STK": "stock", "OPT": "option"}
CLOSE_NOTES = {"Ep": "expired", "A": "assigned", "Ex": "exercised"}
IN_PROGRESS = {"1019"}  # "Statement generation in progress. Please try again shortly."
EASTERN = ZoneInfo("America/New_York")


class NotFlexXML(ValueError):
    pass


class FlexError(RuntimeError):
    """IBKR's Flex Web Service said no (bad or expired token, wrong query ID, ...)."""


def settings_from_env(env: Mapping[str, str]) -> tuple[str, str] | None:
    token = (env.get("IBKR_FLEX_TOKEN") or "").strip()
    query = (env.get("IBKR_FLEX_QUERY_ID") or "").strip()
    return (token, query) if token and query else None


def _root(data: bytes | str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise NotFlexXML("This doesn't look like an IBKR Flex Query XML file.") from exc


def _service_error(root: ET.Element) -> tuple[str, str] | None:
    if root.tag != "FlexStatementResponse" or root.findtext("Status") == "Success":
        return None
    code = (root.findtext("ErrorCode") or "").strip()
    text = (root.findtext("ErrorMessage") or "unknown error").strip()
    return code, text


def _when(text: str) -> str | None:
    text = (text or "").strip()
    for fmt in ("%Y%m%d;%H%M%S", "%Y-%m-%d;%H:%M:%S", "%Y%m%d %H%M%S", "%Y-%m-%d, %H:%M:%S"):
        try:
            local = datetime.strptime(text, fmt).replace(tzinfo=EASTERN)
        except ValueError:
            continue
        return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _expiry(text: str) -> str | None:
    text = (text or "").strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse(data: bytes | str) -> ParseResult:
    root = _root(data)
    err = _service_error(root)
    if err:
        raise FlexError(f"IBKR: {err[1]} (code {err[0]})")
    if root.tag != "FlexQueryResponse":
        raise NotFlexXML("This doesn't look like an IBKR Flex Query XML file.")
    rows = root.findall(".//Trades/Trade")
    if not rows and root.find(".//Trades") is None:
        raise NotFlexXML(
            "The Flex Query has no Trades section. Edit it in Client Portal and tick Trades."
        )

    result = ParseResult()
    staged: list[tuple[str, int, ParsedFill]] = []
    for n, row in enumerate(rows):
        a = row.attrib
        detail = a.get("levelOfDetail", "EXECUTION").upper()
        if detail != "EXECUTION":
            key = f"{detail} rows"
            result.skipped[key] = result.skipped.get(key, 0) + 1
            continue
        category = a.get("assetCategory", "")
        if category not in ASSETS:
            result.skipped[category or "unknown"] = result.skipped.get(category or "unknown", 0) + 1
            continue
        line = f"trade {a.get('tradeID') or n + 1}"
        when = _when(a.get("dateTime") or a.get("tradeDate", ""))
        qty = dec(a.get("quantity"), None)
        price = dec(a.get("tradePrice"), None)
        side = {"BUY": "buy", "SELL": "sell"}.get(a.get("buySell", "").upper())
        if side is None and qty is not None:
            side = "buy" if qty > 0 else "sell"
        if not when or qty is None or qty == 0 or price is None or side is None:
            result.errors.append(f"{line}: missing date, quantity, price or side, skipped")
            continue
        key_id = a.get("tradeID") or a.get("ibExecID") or a.get("transactionID")
        if not key_id:
            result.errors.append(f"{line}: no tradeID, skipped")
            continue
        notes = {part.strip() for part in a.get("notes", "").split(";")}
        note = next((CLOSE_NOTES[c] for c in CLOSE_NOTES if c in notes), None)
        fee = abs(dec(a.get("ibCommission"), ZERO) or ZERO)
        common = dict(
            side=side,
            quantity=abs(qty),
            price=price,
            fee=fee,
            executed_at=when,
            seq=0,
            source_key=f"ibkr:{key_id}",
            note=note,
        )
        if ASSETS[category] == "stock":
            symbol = a.get("symbol", "").strip().upper()
            if not symbol:
                result.errors.append(f"{line}: no symbol, skipped")
                continue
            fill = ParsedFill(asset_class="stock", symbol=symbol, **common)
        else:
            under = (a.get("underlyingSymbol") or "").strip().upper()
            expiry = _expiry(a.get("expiry", ""))
            strike = dec(a.get("strike"), None)
            right = {"C": "call", "P": "put"}.get(a.get("putCall", "").upper())
            if not (under and expiry and strike is not None and right):
                result.errors.append(f"{line}: couldn't read the option contract, skipped")
                continue
            fill = ParsedFill(
                asset_class="option",
                symbol=db.option_symbol(under, expiry, strike, right),
                underlying=under,
                expiry=expiry,
                strike=strike,
                right=right,
                multiplier=dec(a.get("multiplier"), None) or Decimal("100"),
                **common,
            )
        staged.append((when, n, fill))

    # Flex lists trades grouped by symbol; the journal needs them in time order.
    for seq, (_, _, fill) in enumerate(sorted(staged, key=lambda s: (s[0], s[1]))):
        fill.seq = seq
        result.fills.append(fill)
    return result


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "trading-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed https host)
        return resp.read()


def fetch(
    token: str,
    query_id: str,
    http_get: Callable[[str], bytes] = _http_get,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 10,
) -> bytes:
    """Run the Flex Query on IBKR's side and return the report XML."""
    params = urllib.parse.urlencode({"t": token, "q": query_id, "v": 3})
    try:
        first = _root(http_get(f"{SEND_URL}?{params}"))
    except NotFlexXML as exc:
        raise FlexError("IBKR's Flex Web Service sent back something unreadable.") from exc
    err = _service_error(first)
    if err:
        raise FlexError(f"IBKR: {err[1]} (code {err[0]})")
    ref = (first.findtext("ReferenceCode") or "").strip()
    url = (first.findtext("Url") or "").strip()
    if not ref or not url.startswith("https://"):
        raise FlexError("IBKR's Flex Web Service didn't return a report reference.")
    get_params = urllib.parse.urlencode({"t": token, "q": ref, "v": 3})
    for _ in range(attempts):
        data = http_get(f"{url}?{get_params}")
        try:
            root = _root(data)
        except NotFlexXML as exc:
            raise FlexError(
                "IBKR sent back a report that isn't XML. Set the query's format to XML."
            ) from exc
        err = _service_error(root)
        if err is None:
            return data
        if err[0] not in IN_PROGRESS:
            raise FlexError(f"IBKR: {err[1]} (code {err[0]})")
        sleep(3)
    raise FlexError("IBKR is still preparing the report. Try again in a minute.")

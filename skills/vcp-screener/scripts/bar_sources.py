"""Daily-bar data sources for the VCP screener (Alpaca SIP, yfinance, FMP, IBKR).

The FMP path screens from batch quotes, which FMP's free plan refuses. These
sources instead return a year of daily bars per symbol, and the screener
derives price, 52-week range and average volume from those bars. One
``BarsClient`` tries the configured sources in order, records which source
served each symbol, and can cross-check finalists against a second source.

Every source returns rows in the screener's v3 shape, newest first:
``{"date", "open", "high", "low", "close", "adjClose", "volume"}``. Prices are
split-adjusted, not dividend-adjusted, in every source so they compare.
"""

from __future__ import annotations

import csv
import io
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:
    from pathlib import Path as _Path

    print(
        "ERROR: requests library not found. Install this skill's dependencies: "
        f"pip install -r {_Path(__file__).resolve().parents[1] / 'requirements.txt'}",
        file=sys.stderr,
    )
    sys.exit(2)

from coverage_client import history_problem

MARKET_TZ = ZoneInfo("America/New_York")

# Calendar days to request so ~260 sessions come back (252 sessions/year).
LOOKBACK_CALENDAR_DAYS = 400
SESSIONS_KEPT = 260

CONSTITUENTS_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
)

# Cross-check thresholds: a price gap above this is a mismatch; volume is
# looser because consolidated volume legitimately differs a little by vendor.
PRICE_MISMATCH_PCT = 1.0
VOLUME_MISMATCH_PCT = 20.0


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fetch_sp500_constituents() -> list[dict] | None:
    """Public S&P 500 list (same dataset the FMP client falls back to)."""
    try:
        response = requests.get(CONSTITUENTS_CSV_URL, timeout=30)
    except requests.exceptions.RequestException as e:
        print(f"ERROR: S&P 500 list download failed: {type(e).__name__}", file=sys.stderr)
        return None
    if response.status_code != 200:
        print(f"ERROR: S&P 500 list download failed: HTTP {response.status_code}", file=sys.stderr)
        return None
    try:
        rows = [
            {
                "symbol": row["Symbol"].replace(".", "-"),
                "name": row["Security"],
                "sector": row["GICS Sector"],
                "subSector": row["GICS Sub-Industry"],
            }
            for row in csv.DictReader(io.StringIO(response.text))
            if row.get("Symbol")
        ]
    except (csv.Error, KeyError):
        print("ERROR: S&P 500 list had an unexpected format", file=sys.stderr)
        return None
    return rows or None


class SourceError(Exception):
    """A whole source is unusable (bad keys, missing library, blocked)."""


# ---------------------------------------------------------------------------
# Alpaca (SIP feed)
# ---------------------------------------------------------------------------


class AlpacaBarSource:
    """Alpaca Market Data v2 daily bars from the consolidated SIP feed.

    The free plan serves SIP history except the most recent 15 minutes, so the
    request ends 16 minutes ago. The IEX feed is deliberately not used: it
    carries one exchange's volume (a few percent of the total), which would
    break the screener's volume checks.
    """

    name = "alpaca"
    URL = "https://data.alpaca.markets/v2/stocks/bars"
    CHUNK = 50
    RECENT_SIP_EMBARGO = timedelta(minutes=16)

    def __init__(self, key: str, secret: str, session=None, now=None):
        if not key or not secret:
            raise SourceError("ALPACA_API_KEY and ALPACA_SECRET_KEY are not set")
        self.session = session or requests.Session()
        self.session.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        self._now = now
        self.calls = 0

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        return cls(env.get("ALPACA_API_KEY", "").strip(), env.get("ALPACA_SECRET_KEY", "").strip())

    def _utcnow(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def _get(self, params: dict) -> dict:
        for attempt in range(2):
            self.calls += 1
            response = self.session.get(self.URL, params=params, timeout=30)
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429 and attempt == 0:
                time.sleep(float(response.headers.get("Retry-After", 5)))
                continue
            # Alpaca error bodies are short messages (never echo the request,
            # whose headers carry the keys).
            message = (response.text or "")[:160]
            if response.status_code in (401, 403):
                raise SourceError(
                    f"Alpaca refused the request: HTTP {response.status_code} {message}"
                )
            raise RuntimeError(f"Alpaca HTTP {response.status_code} {message}")
        raise RuntimeError("Alpaca rate limit")

    def fetch(self, symbols: list[str], start: date) -> dict[str, list[dict]]:
        end = (self._utcnow() - self.RECENT_SIP_EMBARGO).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: dict[str, list[dict]] = {}
        for i in range(0, len(symbols), self.CHUNK):
            chunk = symbols[i : i + self.CHUNK]
            to_alpaca = {s.replace("-", "."): s for s in chunk}  # BRK-B -> BRK.B
            params = {
                "symbols": ",".join(to_alpaca),
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": end,
                "adjustment": "split",
                "feed": "sip",
                "limit": 10000,
                "sort": "asc",
            }
            raw: dict[str, list[dict]] = {}
            try:
                while True:
                    data = self._get(params)
                    for sym, bars in (data.get("bars") or {}).items():
                        raw.setdefault(sym, []).extend(bars or [])
                    token = data.get("next_page_token")
                    if not token:
                        break
                    params["page_token"] = token
            except SourceError:
                raise
            except (RuntimeError, requests.exceptions.RequestException, ValueError) as e:
                print(f"WARN: Alpaca chunk failed ({type(e).__name__}: {e})", file=sys.stderr)
                continue
            for alp_sym, bars in raw.items():
                ours = to_alpaca.get(alp_sym, alp_sym.replace(".", "-"))
                rows = [r for r in (self._row(b) for b in bars) if r]
                if rows:
                    rows.sort(key=lambda r: r["date"], reverse=True)
                    out[ours] = rows
        return out

    @staticmethod
    def _row(bar: dict) -> dict | None:
        try:
            ts = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
            day = ts.astimezone(MARKET_TZ).date().isoformat()
            row = {
                "date": day,
                "open": bar["o"],
                "high": bar["h"],
                "low": bar["l"],
                "close": bar["c"],
                "adjClose": bar["c"],
                "volume": bar["v"],
            }
        except (KeyError, TypeError, ValueError):
            return None
        return row


# ---------------------------------------------------------------------------
# Interactive Brokers (IBKR) through IB Gateway: cross-check only
# ---------------------------------------------------------------------------


def _load_ib_async():
    """Import ib_async lazily; None when not installed (IBKR is optional)."""
    try:
        import ib_async  # optional dependency
    except ImportError:
        return None
    return ib_async


class IbkrBarSource:
    """Daily TRADES bars from IB Gateway (split-adjusted, like Alpaca's split adjustment).

    Used only to cross-check the stocks a screen shows. IBKR paces historical
    requests (about 60 per 10 minutes), so it is too slow to screen the whole
    S&P 500 but fine for the 10-30 finalists. Read-only: connects with readonly=True.
    Enabled by IBKR_HOST (the ib-gateway service name); IBKR_MODE=live uses port 4003.
    """

    name = "ibkr"
    CLIENT_ID = 50  # the dashboard's web workers use 100+

    def __init__(self, host: str, port: int = 4004, ib_module=None, now=None, timeout: int = 10):
        if not host:
            raise SourceError("IBKR_HOST is not set")
        if ib_module is None:
            ib_module = _load_ib_async()
        if ib_module is None:
            raise SourceError("ib_async is not installed")
        self.ib = ib_module
        self.host, self.port, self.timeout = host, port, timeout
        self._now = now
        self.calls = 0

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        live = env.get("IBKR_MODE", "").strip().lower() == "live"
        return cls(env.get("IBKR_HOST", "").strip(), 4003 if live else 4004)

    def _now_et(self) -> datetime:
        return (self._now or datetime.now(timezone.utc)).astimezone(MARKET_TZ)

    def fetch(self, symbols: list[str], start: date) -> dict[str, list[dict]]:
        now = self._now_et()
        session_over = (now.hour, now.minute) >= (16, 0)
        days = max(2, (now.date() - start).days + 1)
        duration = f"{days} D" if days <= 365 else f"{math.ceil(days / 365)} Y"
        ib = self.ib.IB()
        try:
            ib.connect(
                self.host, self.port, clientId=self.CLIENT_ID, readonly=True, timeout=self.timeout
            )
        except (ConnectionError, OSError, TimeoutError) as e:
            raise SourceError(
                f"IB Gateway at {self.host}:{self.port} isn't answering ({type(e).__name__})"
            ) from e
        out: dict[str, list[dict]] = {}
        try:
            for sym in symbols:
                self.calls += 1
                contract = self.ib.Stock(sym.replace("-", " "), "SMART", "USD")  # BRK-B -> BRK B
                try:
                    found = [c for c in (ib.qualifyContracts(contract) or []) if c is not None]
                    if not found:
                        continue
                    raw = ib.reqHistoricalData(found[0], "", duration, "1 day", "TRADES", True)
                except Exception as e:  # ib_async raises several types on bad symbols/timeouts
                    print(f"WARN: IBKR bars for {sym} failed ({type(e).__name__})", file=sys.stderr)
                    continue
                rows = []
                for b in raw or []:
                    d = b.date
                    if isinstance(d, datetime):
                        d = d.astimezone(MARKET_TZ).date()
                    if d == now.date() and not session_over:
                        continue  # today's bar is still forming
                    if not _finite(b.close) or b.close <= 0:
                        continue
                    rows.append(
                        {
                            "date": d.isoformat(),
                            "open": b.open,
                            "high": b.high,
                            "low": b.low,
                            "close": b.close,
                            "adjClose": b.close,
                            "volume": float(b.volume),
                        }
                    )
                if rows:
                    rows.sort(key=lambda r: r["date"], reverse=True)
                    out[sym] = rows
        finally:
            ib.disconnect()
        return out


# ---------------------------------------------------------------------------
# yfinance (Yahoo Finance, unofficial)
# ---------------------------------------------------------------------------


def _load_yfinance():
    """Import yfinance lazily (it pulls in pandas); None when not installed."""
    try:
        import yfinance  # optional dependency
    except ImportError:
        return None
    return yfinance


class YahooBarSource:
    """Daily bars via yfinance. Unofficial: used as a backup and a cross-check."""

    name = "yfinance"
    CHUNK = 100

    def __init__(self, yf_module=None):
        if yf_module is None:
            yf_module = _load_yfinance()
        if yf_module is None:
            raise SourceError("yfinance is not installed")
        self.yf = yf_module
        self.calls = 0

    def fetch(self, symbols: list[str], start: date) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        end = (datetime.now(MARKET_TZ).date() + timedelta(days=1)).isoformat()
        for i in range(0, len(symbols), self.CHUNK):
            chunk = symbols[i : i + self.CHUNK]
            self.calls += 1
            try:
                frame = self.yf.download(
                    tickers=chunk,
                    start=start.isoformat(),
                    end=end,
                    interval="1d",
                    auto_adjust=False,  # Close is split-adjusted only, like Alpaca
                    actions=False,
                    group_by="ticker",
                    threads=True,
                    progress=False,
                )
            except Exception as e:  # yfinance raises many types on Yahoo changes
                print(f"WARN: yfinance chunk failed ({type(e).__name__}: {e})", file=sys.stderr)
                continue
            if frame is None or getattr(frame, "empty", True):
                continue
            for sym in chunk:
                rows = self._rows(frame, sym, multi=len(chunk) > 1)
                if rows:
                    out[sym] = rows
        return out

    @staticmethod
    def _rows(frame, sym: str, multi: bool) -> list[dict]:
        try:
            columns = frame.columns
            if getattr(columns, "nlevels", 1) > 1:
                if sym not in columns.get_level_values(0):
                    return []
                sub = frame[sym]
            elif multi:
                return []
            else:
                sub = frame
        except (KeyError, AttributeError):
            return []
        rows = []
        for idx, rec in sub.iterrows():
            close = rec.get("Close")
            if not _finite(close):
                continue
            volume = rec.get("Volume")
            rows.append(
                {
                    "date": idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10],
                    "open": float(rec.get("Open")),
                    "high": float(rec.get("High")),
                    "low": float(rec.get("Low")),
                    "close": float(close),
                    "adjClose": float(close),
                    "volume": int(volume) if _finite(volume) else volume,
                }
            )
        rows.sort(key=lambda r: r["date"], reverse=True)
        return rows


# ---------------------------------------------------------------------------
# FMP (last resort; one call per symbol)
# ---------------------------------------------------------------------------


class FMPBarSource:
    """Per-symbol FMP history. Costs one call each, so it only gets leftovers."""

    name = "fmp"

    def __init__(self, fmp_client):
        self.client = fmp_client
        self.calls = 0

    def fetch(self, symbols: list[str], start: date) -> dict[str, list[dict]]:
        out = {}
        for sym in symbols:
            self.calls += 1
            data = self.client.get_historical_prices(sym, days=SESSIONS_KEPT)
            rows = data.get("historical", []) if isinstance(data, dict) else []
            if rows:
                out[sym] = rows
        return out


# ---------------------------------------------------------------------------
# Client the screener talks to
# ---------------------------------------------------------------------------


def quote_from_history(rows: list[dict]) -> dict | None:
    """Build the pre-filter's quote fields from newest-first daily bars."""
    year = rows[:252]
    if not year:
        return None
    highs = [r.get("high") for r in year if _finite(r.get("high"))]
    lows = [r.get("low") for r in year if _finite(r.get("low"))]
    volumes = [r.get("volume") for r in rows[:50] if _finite(r.get("volume"))]
    price = year[0].get("close")
    if not highs or not lows or not volumes or not _finite(price):
        return None
    return {
        "price": price,
        "yearHigh": max(highs),
        "yearLow": min(lows),
        "avgVolume": sum(volumes) / len(volumes),
        "volume": year[0].get("volume"),
    }


def compare_latest(primary: list[dict], other: list[dict]) -> dict:
    """Compare the newest common session of two newest-first bar lists."""
    if not other:
        return {"status": "unavailable"}
    other_by_date = {r.get("date"): r for r in other}
    common = next((r for r in primary if r.get("date") in other_by_date), None)
    if common is None:
        return {"status": "unavailable"}
    o = other_by_date[common["date"]]
    result = {"date": common["date"]}
    status = "ok"
    if _finite(common.get("close")) and _finite(o.get("close")) and o["close"] > 0:
        diff = abs(common["close"] - o["close"]) / o["close"] * 100
        result["price_diff_pct"] = round(diff, 2)
        if diff > PRICE_MISMATCH_PCT:
            status = "mismatch"
    else:
        return {"status": "unavailable"}
    if _finite(common.get("volume")) and _finite(o.get("volume")) and o["volume"] > 0:
        vdiff = abs(common["volume"] - o["volume"]) / o["volume"] * 100
        result["volume_diff_pct"] = round(vdiff, 1)
        if vdiff > VOLUME_MISMATCH_PCT and status == "ok":
            status = "volume_mismatch"
    # One source missing the newest session means one of them is stale.
    newest_primary = primary[0].get("date") if primary else None
    newest_other = other[0].get("date")
    if status == "ok" and newest_primary != newest_other:
        status = "stale"
        result["newest"] = {"primary": newest_primary, "other": newest_other}
    result["status"] = status
    return result


class BarsClient:
    """Duck-types the FMP client methods screen_vcp uses, backed by daily bars."""

    histories_prefetched = True

    def __init__(self, sources: list, cross_check_sources: list | None = None, today=None):
        if not sources:
            raise SourceError("no usable data source")
        self.sources = sources
        self.cross_check_sources = cross_check_sources or []
        self.today = today or datetime.now(MARKET_TZ).date()
        self.histories: dict[str, list[dict]] = {}
        self.source_of: dict[str, str] = {}
        self.coverage_failures: dict = {}
        self.source_errors: dict[str, str] = {}
        self.single_symbol_quotes = False
        self.unconfigured: list[str] = []

    @property
    def start(self) -> date:
        return self.today - timedelta(days=LOOKBACK_CALENDAR_DAYS)

    def get_sp500_constituents(self):
        return fetch_sp500_constituents()

    def _prefetch(self, symbols: list[str]) -> None:
        remaining = [s for s in dict.fromkeys(symbols) if s not in self.histories]
        best: dict[str, tuple[str, list[dict]]] = {}
        for source in self.sources:
            if not remaining or source.name in self.source_errors:
                continue
            print(f"  [{source.name}] fetching {len(remaining)} symbols...", flush=True)
            try:
                got = source.fetch(remaining, self.start)
            except SourceError as e:
                self.source_errors[source.name] = str(e)
                print(f"WARN: {source.name} unavailable: {e}", file=sys.stderr)
                continue
            if not got and len(remaining) > 5:
                # yfinance in particular reports a block as "no data" per symbol.
                self.source_errors[source.name] = "returned no data"
                print(f"WARN: {source.name} returned no data", file=sys.stderr)
            still = []
            for sym in remaining:
                rows = got.get(sym)
                if rows and history_problem(rows[:SESSIONS_KEPT]) is None:
                    self.histories[sym] = rows[:SESSIONS_KEPT]
                    self.source_of[sym] = source.name
                else:
                    if rows and sym not in best:
                        best[sym] = (source.name, rows[:SESSIONS_KEPT])
                    still.append(sym)
            remaining = still
        for sym in remaining:
            if sym in best:  # short history (recent IPO): let the screener classify it
                self.source_of[sym], self.histories[sym] = best[sym]
            else:
                reason = "provider_error" if self.source_errors else "missing_data"
                self.coverage_failures[("quote", sym)] = reason
                self.coverage_failures[("historical", sym)] = reason

    def get_batch_quotes(self, symbols: list[str]) -> dict[str, dict]:
        self._prefetch(list(symbols) + ["SPY"])
        quotes = {}
        for sym in symbols:
            rows = self.histories.get(sym)
            q = quote_from_history(rows) if rows else None
            if q:
                quotes[sym] = {"symbol": sym, **q}
            elif rows:
                self.coverage_failures[("quote", sym)] = "invalid_data"
        return quotes

    def get_historical_prices(self, symbol: str, days: int = 365) -> dict | None:
        if symbol not in self.histories:
            self._prefetch([symbol])
        rows = self.histories.get(symbol)
        if not rows:
            return None
        return {"symbol": symbol, "historical": rows[:days]}

    def cross_check(self, symbols: list[str]) -> dict[str, dict]:
        """Compare each symbol's newest session against a different source."""
        results: dict[str, dict] = {}
        for source in self.cross_check_sources:
            todo = [
                s
                for s in symbols
                if s not in results and self.source_of.get(s) not in (None, source.name)
            ]
            if not todo:
                continue
            try:
                other = source.fetch(todo, self.today - timedelta(days=14))
            except SourceError as e:
                print(f"WARN: cross-check via {source.name} unavailable: {e}", file=sys.stderr)
                continue
            for sym in todo:
                check = compare_latest(self.histories.get(sym, []), other.get(sym, []))
                if check["status"] != "unavailable":
                    check["other_source"] = source.name
                    results[sym] = check
        for sym in symbols:
            results.setdefault(sym, {"status": "unavailable"})
        return results

    def get_api_stats(self) -> dict:
        counts: dict[str, int] = {}
        for name in self.source_of.values():
            counts[name] = counts.get(name, 0) + 1
        return {
            "api_calls_made": sum(getattr(s, "calls", 0) for s in self.sources),
            "cache_entries": len(self.histories),
            "rate_limit_reached": False,
            "data_sources": counts,
            "source_errors": dict(self.source_errors),
            "unconfigured_sources": list(self.unconfigured),
        }


def build_bars_client(data_source: str, fmp_client=None, env=None) -> BarsClient:
    """auto = Alpaca SIP, then yfinance, then FMP; cross-check on the others.

    When IBKR_HOST is set, IBKR is the first cross-check source (yfinance and
    Alpaca cover any symbol IBKR can't).
    """
    env = os.environ if env is None else env

    def make(name):
        try:
            if name == "alpaca":
                return AlpacaBarSource.from_env(env)
            if name == "yfinance":
                return YahooBarSource()
            if name == "fmp":
                if fmp_client is None:
                    raise SourceError("FMP_API_KEY is not set")
                return FMPBarSource(fmp_client)
        except SourceError as e:
            print(f"WARN: {name} unavailable: {e}", file=sys.stderr)
        return None

    order = {
        "auto": ["alpaca", "yfinance", "fmp"],
        "alpaca": ["alpaca"],
        "yfinance": ["yfinance"],
    }[data_source]
    built = {name: make(name) for name in dict.fromkeys(order + ["alpaca", "yfinance"])}
    sources = [built[n] for n in order if built.get(n)]
    checkers = [built[n] for n in ("yfinance", "alpaca") if built.get(n)]
    if env.get("IBKR_HOST", "").strip():
        try:
            checkers.insert(0, IbkrBarSource.from_env(env))
        except SourceError as e:
            print(f"WARN: ibkr cross-check unavailable: {e}", file=sys.stderr)
    client = BarsClient(sources, cross_check_sources=checkers)
    client.unconfigured = [name for name in order if built.get(name) is None]
    return client

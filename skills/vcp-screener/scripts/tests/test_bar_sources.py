"""Offline tests for the daily-bar sources (Alpaca SIP, yfinance, FMP) and cross-check."""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
import screen_vcp
from bar_sources import (
    AlpacaBarSource,
    BarsClient,
    SourceError,
    YahooBarSource,
    compare_latest,
    quote_from_history,
)

LAST = date(2026, 11, 13)


def bars(n=260, close=100.0, volume=500000, last=LAST):
    """Newest-first rows on consecutive calendar days (fine for these tests)."""
    return [
        {
            "date": (last - timedelta(days=i)).isoformat(),
            "open": close - i * 0.1,
            "high": close + 2 - i * 0.1,
            "low": close - 2 - i * 0.1,
            "close": close - i * 0.1,
            "adjClose": close - i * 0.1,
            "volume": volume,
        }
        for i in range(n)
    ]


class FakeSource:
    def __init__(self, name, data=None, error=None):
        self.name = name
        self.data = data or {}
        self.error = error
        self.calls = 0
        self.requested = []

    def fetch(self, symbols, start):
        self.calls += 1
        self.requested.append(list(symbols))
        if self.error:
            raise SourceError(self.error)
        return {s: self.data[s] for s in symbols if s in self.data}


# ---------------------------------------------------------------------------
# Alpaca
# ---------------------------------------------------------------------------


def test_alpaca_requests_split_adjusted_sip_ending_before_embargo():
    seen = []

    def get(url, params=None, timeout=None):
        seen.append(dict(params))
        body = {
            "bars": {
                "BRK.B": [
                    {"t": "2026-11-12T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10},
                    {"t": "2026-11-13T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.6, "v": 11},
                ]
            },
            "next_page_token": None,
        }
        return SimpleNamespace(status_code=200, json=lambda: body, headers={}, text="")

    session = SimpleNamespace(get=get, headers={})
    now = datetime(2026, 11, 13, 21, 20, tzinfo=timezone.utc)
    src = AlpacaBarSource("k", "s", session=session, now=now)  # pragma: allowlist secret
    out = src.fetch(["BRK-B"], date(2025, 10, 9))

    params = seen[0]
    assert params["feed"] == "sip"
    assert params["adjustment"] == "split"
    assert params["symbols"] == "BRK.B"
    assert params["end"] == "2026-11-13T21:04:00Z"  # 16 minutes before now
    assert list(out) == ["BRK-B"]
    assert [r["date"] for r in out["BRK-B"]] == ["2026-11-13", "2026-11-12"]
    assert out["BRK-B"][0]["close"] == 1.6


def test_alpaca_follows_pagination():
    pages = [
        {
            "bars": {
                "AAA": [{"t": "2026-11-12T05:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]
            },
            "next_page_token": "p2",
        },
        {
            "bars": {
                "AAA": [{"t": "2026-11-13T05:00:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 2}]
            },
            "next_page_token": None,
        },
    ]
    tokens = []

    def get(url, params=None, timeout=None):
        tokens.append(params.get("page_token"))
        page = pages[len(tokens) - 1]
        return SimpleNamespace(status_code=200, json=lambda: page, headers={}, text="")

    src = AlpacaBarSource(
        "k", "s", session=SimpleNamespace(get=get, headers={})
    )  # pragma: allowlist secret
    out = src.fetch(["AAA"], date(2026, 1, 1))
    assert tokens == [None, "p2"]
    assert [r["close"] for r in out["AAA"]] == [2, 1]


def test_alpaca_bad_keys_disable_the_source():
    def get(url, params=None, timeout=None):
        return SimpleNamespace(status_code=403, json=lambda: {}, headers={}, text="forbidden")

    src = AlpacaBarSource(
        "k", "s", session=SimpleNamespace(get=get, headers={})
    )  # pragma: allowlist secret
    with pytest.raises(SourceError):
        src.fetch(["AAA"], date(2026, 1, 1))


def test_alpaca_requires_keys():
    with pytest.raises(SourceError):
        AlpacaBarSource.from_env({})


# ---------------------------------------------------------------------------
# yfinance
# ---------------------------------------------------------------------------


def _yf_frame(symbols, closes):
    idx = pd.to_datetime(["2026-11-12", "2026-11-13"])
    cols = pd.MultiIndex.from_product([symbols, ["Open", "High", "Low", "Close", "Volume"]])
    data = []
    for c in closes:
        row = []
        for _ in symbols:
            row += [c, c + 1, c - 1, c, 1000]
        data.append(row)
    return pd.DataFrame(data, index=idx, columns=cols)


def test_yahoo_parses_multi_ticker_frame_newest_first():
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        return _yf_frame(kwargs["tickers"], [10.0, 11.0])

    src = YahooBarSource(yf_module=SimpleNamespace(download=download))
    out = src.fetch(["AAA", "BRK-B"], date(2026, 1, 1))
    assert calls[0]["auto_adjust"] is False  # split-adjusted Close, like Alpaca
    assert set(out) == {"AAA", "BRK-B"}
    assert out["AAA"][0] == {
        "date": "2026-11-13",
        "open": 11.0,
        "high": 12.0,
        "low": 10.0,
        "close": 11.0,
        "adjClose": 11.0,
        "volume": 1000,
    }


def test_yahoo_download_error_returns_nothing():
    def download(**kwargs):
        raise RuntimeError("Yahoo changed something")

    src = YahooBarSource(yf_module=SimpleNamespace(download=download))
    assert src.fetch(["AAA"], date(2026, 1, 1)) == {}


# ---------------------------------------------------------------------------
# BarsClient
# ---------------------------------------------------------------------------


def test_falls_back_per_symbol_and_records_source():
    alpaca = FakeSource("alpaca", {"AAA": bars(), "SPY": bars()})
    yahoo = FakeSource("yfinance", {"BBB": bars()})
    client = BarsClient([alpaca, yahoo], today=LAST)
    quotes = client.get_batch_quotes(["AAA", "BBB", "CCC"])
    assert set(quotes) == {"AAA", "BBB"}
    assert client.source_of == {"AAA": "alpaca", "SPY": "alpaca", "BBB": "yfinance"}
    assert yahoo.requested == [["BBB", "CCC"]]  # only what Alpaca missed
    assert client.coverage_failures[("quote", "CCC")] == "missing_data"
    assert client.get_api_stats()["data_sources"] == {"alpaca": 2, "yfinance": 1}


def test_source_failure_marks_leftovers_provider_error():
    alpaca = FakeSource("alpaca", error="HTTP 403")
    client = BarsClient([alpaca], today=LAST)
    assert client.get_batch_quotes(["AAA"]) == {}
    assert client.coverage_failures[("quote", "AAA")] == "provider_error"
    assert client.get_api_stats()["source_errors"] == {"alpaca": "HTTP 403"}


def test_empty_answer_for_many_symbols_counts_as_source_failure():
    yahoo = FakeSource("yfinance", {})
    client = BarsClient([yahoo], today=LAST)
    client.get_batch_quotes([f"S{i}" for i in range(10)])
    assert client.source_errors == {"yfinance": "returned no data"}


def test_short_history_falls_through_then_is_kept_for_classification():
    alpaca = FakeSource("alpaca", {"NEW": bars(n=40)})
    yahoo = FakeSource("yfinance", {})
    client = BarsClient([alpaca, yahoo], today=LAST)
    client.get_batch_quotes(["NEW"])
    assert client.source_of["NEW"] == "alpaca"
    assert len(client.get_historical_prices("NEW", 260)["historical"]) == 40


def test_quote_from_history_uses_year_range_and_50_day_volume():
    rows = bars()
    rows[0]["volume"] = 1_000_000
    q = quote_from_history(rows)
    assert q["price"] == rows[0]["close"]
    assert q["yearHigh"] == max(r["high"] for r in rows[:252])
    assert q["yearLow"] == min(r["low"] for r in rows[:252])
    assert q["avgVolume"] == pytest.approx((1_000_000 + 49 * 500000) / 50)


# ---------------------------------------------------------------------------
# Cross-check
# ---------------------------------------------------------------------------


def test_compare_latest_statuses():
    a = bars(n=5)
    assert compare_latest(a, bars(n=5))["status"] == "ok"
    assert compare_latest(a, bars(n=5, close=103))["status"] == "mismatch"
    assert compare_latest(a, bars(n=5, volume=800000))["status"] == "volume_mismatch"
    stale = bars(n=5, last=LAST - timedelta(days=1))
    assert compare_latest(a, stale)["status"] == "stale"
    assert compare_latest(a, [])["status"] == "unavailable"


def test_cross_check_uses_a_different_source_than_the_primary():
    alpaca = FakeSource("alpaca", {"AAA": bars(), "BBB": bars()})
    yahoo_rows = {"AAA": bars(close=102), "BBB": bars()}
    yahoo = FakeSource("yfinance", yahoo_rows)
    client = BarsClient([alpaca, yahoo], cross_check_sources=[yahoo, alpaca], today=LAST)
    client.get_batch_quotes(["AAA", "BBB"])
    checks = client.cross_check(["AAA", "BBB"])
    assert checks["AAA"]["status"] == "mismatch"
    assert checks["AAA"]["other_source"] == "yfinance"
    assert checks["BBB"]["status"] == "ok"


# ---------------------------------------------------------------------------
# End to end through screen_vcp
# ---------------------------------------------------------------------------


def test_screen_with_daily_bars_records_source_and_cross_check(monkeypatch, tmp_path):
    alpaca = FakeSource("alpaca", {"AAA": bars(), "SPY": bars()})
    yahoo = FakeSource("yfinance", {"AAA": bars()})
    client = BarsClient([alpaca, yahoo], cross_check_sources=[yahoo, alpaca], today=LAST)
    monkeypatch.setattr(screen_vcp, "build_bars_client", lambda *a, **k: client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "screen_vcp.py",
            "--data-source",
            "auto",
            "--universe",
            "AAA",
            "BBB",
            "--output-dir",
            str(tmp_path),
        ],
    )
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    screen_vcp.main()
    report = json.loads(next(tmp_path.glob("*.json")).read_text())
    meta = report["metadata"]
    assert meta["data_source"] == "auto"
    assert meta["api_stats"]["data_sources"] == {"alpaca": 2}
    cov = report["evaluation_coverage"]
    assert cov["not_evaluated_reasons"].get("missing_data") == 1  # BBB
    assert report["results"]
    for r in report["results"]:
        assert r["data_source"] == "alpaca"
        assert r["cross_check"]["status"] == "ok"


def test_yahoo_source_unavailable_without_yfinance(monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", None)
    with pytest.raises(SourceError):
        YahooBarSource()

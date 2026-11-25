from datetime import datetime

import pytest
from app import chart_page, create_app
from app.ibkr import ET, IbkrError


class Session:
    def __init__(self, r):
        self.r = r

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stock_quote(self, symbol):
        if self.r.down:
            raise IbkrError("IB Gateway isn't answering")
        return {"symbol": symbol, "price": 101.0, "prev_close": 100.0, "data": "delayed"}

    def bars(self, symbol, duration, size):
        self.r.asked.append((duration, size))
        return [
            {
                "t": datetime(2026, 11, 25, 9, 30, tzinfo=ET),
                "open": 100,
                "high": 100.5,
                "low": 99.5,
                "close": 100.2,
                "volume": 1000,
            },
            {
                "t": datetime(2026, 11, 25, 9, 35, tzinfo=ET),
                "open": 100.2,
                "high": 101.4,
                "low": 100.1,
                "close": 101.0,
                "volume": 2000,
            },
        ]


class Reader:
    def __init__(self, down=False):
        self.down = down
        self.asked = []

    def session(self):
        return Session(self)


class Alpaca:
    paper = True

    def __init__(self, price):
        self.price = price

    def stock_quote(self, symbol):
        return {"price": self.price}


def make(reports_dir, state_dir, ib, alpaca=None):
    a = create_app(
        {
            "REPORTS_DIR": reports_dir,
            "STATE_DIR": state_dir,
            "ALPACA": alpaca,
            "IBKR": ib,
            "JOURNAL_DIR": None,
        }
    )
    a.testing = True
    return a.test_client()


def test_summary_against_previous_close():
    bars = [
        {"close": 100.2, "high": 100.5, "low": 99.5, "volume": 1},
        {"close": 101.0, "high": 101.4, "low": 100.1, "volume": 2},
    ]
    s = chart_page.summarize(bars, 100.0)
    assert s["change"] == pytest.approx(1.0) and s["change_pct"] == pytest.approx(1.0)
    assert s["high"] == 101.4 and s["low"] == 99.5 and s["volume"] == 3


def test_page_without_ibkr_says_so(reports_dir, state_dir):
    body = make(reports_dir, state_dir, None).get("/chart/?symbol=SPY").get_data(as_text=True)
    assert "IBKR is off" in body


def test_intraday_chart_renders(reports_dir, state_dir):
    ib = Reader()
    body = (
        make(reports_dir, state_dir, ib, Alpaca(101.05))
        .get("/chart/?symbol=spy")
        .get_data(as_text=True)
    )
    assert ib.asked == [("1 D", "5 mins")]
    assert "<svg" in body and "09:35" in body and "+1.00 (+1.00%)" in body
    assert "agrees within" in body


def test_range_switch_and_bad_range(reports_dir, state_dir):
    ib = Reader()
    c = make(reports_dir, state_dir, ib)
    c.get("/chart/?symbol=SPY&range=3m")
    c.get("/chart/?symbol=SPY&range=bogus")
    assert ib.asked == [("3 M", "1 day"), ("1 D", "5 mins")]


def test_mismatch_flag_and_gateway_down(reports_dir, state_dir):
    body = (
        make(reports_dir, state_dir, Reader(), Alpaca(104.0))
        .get("/chart/?symbol=SPY")
        .get_data(as_text=True)
    )
    assert "check another chart" in body
    body = (
        make(reports_dir, state_dir, Reader(down=True))
        .get("/chart/?symbol=SPY")
        .get_data(as_text=True)
    )
    assert "isn&#39;t answering" in body

import pytest
from app import ibkr


def test_not_configured_without_host():
    assert ibkr.settings_from_env({}) is None


def test_defaults_to_paper_port_and_delayed_data():
    s = ibkr.settings_from_env({"IBKR_HOST": "ib-gateway"})
    assert s.host == "ib-gateway"
    assert s.port == 4004
    assert s.market_data_type == 3
    assert s.paper is True


def test_live_mode_switches_port():
    s = ibkr.settings_from_env({"IBKR_HOST": "ib-gateway", "IBKR_MODE": "LIVE"})
    assert s.port == 4003
    assert s.paper is False


@pytest.mark.parametrize("value", ["", "paper", "typo"])
def test_anything_but_live_stays_paper(value):
    s = ibkr.settings_from_env({"IBKR_HOST": "ib-gateway", "IBKR_MODE": value})
    assert s.paper is True


def test_live_market_data_opt_in():
    s = ibkr.settings_from_env({"IBKR_HOST": "x", "IBKR_MARKET_DATA": "live"})
    assert s.market_data_type == 1


def test_rejects_bad_symbol():
    s = ibkr.settings_from_env({"IBKR_HOST": "x"})
    with pytest.raises(ValueError):
        ibkr.fetch_quote(s, "spy; rm")


def test_fetch_quote_uses_fake_client():
    class FakeTicker:
        last = 600.5
        bid = 600.4
        ask = 600.6
        close = 598.0
        marketDataType = 3

    class FakeIB:
        def __init__(self):
            self.calls = []

        def connect(self, host, port, clientId, readonly, timeout):
            self.calls.append(("connect", host, port, clientId, readonly))

        def reqMarketDataType(self, t):
            self.calls.append(("type", t))

        def qualifyContracts(self, c):
            return [c]

        def reqMktData(self, c, snapshot):
            self.calls.append(("mkt", c.symbol, snapshot))
            return FakeTicker()

        def sleep(self, s):
            pass

        def disconnect(self):
            self.calls.append(("disconnect",))

    fake = FakeIB()
    s = ibkr.settings_from_env({"IBKR_HOST": "ib-gateway"})
    q = ibkr.fetch_quote(s, "SPY", ib_factory=lambda: fake)
    assert q["symbol"] == "SPY"
    assert q["last"] == 600.5
    assert q["data"] == "delayed"
    assert ("connect", "ib-gateway", 4004, 17, True) in fake.calls
    assert ("type", 3) in fake.calls
    assert fake.calls[-1] == ("disconnect",)


# ---- reader used by the web pages ----

from datetime import date, datetime  # noqa: E402
from types import SimpleNamespace as NS  # noqa: E402

NAN = float("nan")


class FakeGateway:
    """Stands in for ib_async.IB. Records calls; returns canned data."""

    def __init__(self, fail_connect=False):
        self.calls = []
        self.fail_connect = fail_connect
        self.connections = 0

    def connect(self, host, port, clientId, readonly, timeout):
        if self.fail_connect:
            raise TimeoutError()
        self.connections += 1
        self.calls.append(("connect", readonly))

    def reqMarketDataType(self, t):
        self.calls.append(("type", t))

    def qualifyContracts(self, c):
        if getattr(c, "lastTradeDateOrContractMonth", "") == "20991231":
            return []
        c.conId = 756733
        return [c]

    def reqMktData(self, c, *a, **k):
        self.calls.append(("mkt", c.symbol, getattr(c, "right", "")))
        if getattr(c, "right", ""):
            return NS(
                bid=2.10,
                ask=2.30,
                last=2.2,
                close=NAN,
                marketDataType=3,
                modelGreeks=NS(impliedVol=0.31, delta=0.52, gamma=0.04, theta=-0.05, vega=0.12),
            )
        return NS(bid=99.9, ask=100.1, last=NAN, close=98.0, marketDataType=3)

    def cancelMktData(self, c):
        pass

    def reqHistoricalData(self, c, end, dur, size, what, rth):
        self.calls.append(("hist", dur, size))
        return [
            NS(date=date(2026, 11, 23), close=97.0),
            NS(date=date(2026, 11, 24), close=98.0),
            NS(date=date(2026, 11, 25), close=99.5),  # today, still forming
        ]

    def reqSecDefOptParams(self, sym, fut_exch, sec_type, con_id):
        return [
            NS(
                exchange="SMART", expirations={"20261204", "20261127"}, strikes={95.0, 100.0, 105.0}
            ),
            NS(exchange="CBOE", expirations={"20270115"}, strikes={50.0}),
        ]

    def sleep(self, s):
        pass

    def disconnect(self):
        self.calls.append(("disconnect",))


MIDDAY = datetime(2026, 11, 25, 12, 0, tzinfo=ibkr.ET)
AFTER_CLOSE = datetime(2026, 11, 25, 17, 0, tzinfo=ibkr.ET)


def make_reader(gw, clock=MIDDAY):
    s = ibkr.settings_from_env({"IBKR_HOST": "ib-gateway"})
    return ibkr.IbkrReader(s, ib_factory=lambda: gw, clock=lambda: clock)


def test_reader_from_env_off_without_host():
    assert ibkr.reader_from_env({}) is None
    assert ibkr.reader_from_env({"IBKR_HOST": "ib-gateway"}).paper is True


def test_stock_quote_uses_midpoint_and_drops_todays_partial_bar():
    gw = FakeGateway()
    with make_reader(gw).session() as s:
        q = s.stock_quote("spy")
    assert q["price"] == 100.0  # no last trade -> bid/ask midpoint
    assert q["closes"] == [97.0, 98.0]
    assert q["source"] == "IBKR" and q["data"] == "delayed"
    assert ("connect", True) in gw.calls and gw.calls[-1] == ("disconnect",)


def test_todays_bar_kept_after_the_close():
    gw = FakeGateway()
    with make_reader(gw, AFTER_CLOSE).session() as s:
        assert s.stock_quote("SPY")["closes"] == [97.0, 98.0, 99.5]


def test_quotes_are_cached_and_need_no_new_connection():
    gw = FakeGateway()
    reader = make_reader(gw)
    with reader.session() as s:
        s.stock_quote("SPY")
    with reader.session() as s:
        s.stock_quote("SPY")
    assert gw.connections == 1


def test_option_chain_prefers_smart_exchange():
    gw = FakeGateway()
    with make_reader(gw).session() as s:
        chain = s.option_chain("SPY")
    assert chain["expirations"] == [date(2026, 11, 27), date(2026, 12, 4)]
    assert chain["strikes"] == [95.0, 100.0, 105.0]


def test_option_quote_has_mid_and_ibkr_greeks():
    gw = FakeGateway()
    with make_reader(gw).session() as s:
        q = s.option_quote("SPY", date(2026, 12, 4), 100, "c")
    assert q["mid"] == pytest.approx(2.20)
    assert q["iv"] == 0.31 and q["delta"] == 0.52 and q["theta"] == -0.05


def test_unknown_option_contract_is_an_ibkr_error():
    with make_reader(FakeGateway()).session() as s:
        with pytest.raises(ibkr.IbkrError):
            s.option_quote("SPY", date(2099, 12, 31), 100, "P")


def test_gateway_down_is_an_ibkr_error():
    with make_reader(FakeGateway(fail_connect=True)).session() as s:
        with pytest.raises(ibkr.IbkrError, match="isn't answering"):
            s.stock_quote("SPY")


def test_bars_converts_and_skips_bad_rows():
    gw = FakeGateway()
    with make_reader(gw).session() as s:
        bars = s.bars("SPY", "3 M", "1 day")
    assert [b["close"] for b in bars] == [97.0, 98.0, 99.5]
    assert ("hist", "3 M", "1 day") in gw.calls

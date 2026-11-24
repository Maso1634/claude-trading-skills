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

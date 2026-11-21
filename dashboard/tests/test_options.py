import json
import sys
from datetime import date
from pathlib import Path

import pytest
from app import alpaca, create_app
from app.options import pricing
from app.options.strategies import Inputs, analyze

from .test_journal_app import new_trade

SKILL_SCRIPTS = Path(__file__).resolve().parents[2] / "skills/options-strategy-advisor/scripts"
TODAY = date(2026, 11, 20)


def inputs(**over):
    base = dict(
        strategy="long_call",
        price=100.0,
        expiry=date(2026, 12, 18),
        today=TODAY,
        strike=100.0,
        premium=3.0,
        iv=0.30,
        rate=0.04,
    )
    base.update(over)
    return Inputs(**base)


# ---- pricing ----


@pytest.mark.parametrize("right", ["call", "put"])
@pytest.mark.parametrize(
    "S,K,T,sigma,q", [(100, 100, 1.0, 0.2, 0.0), (50, 55, 30 / 365, 0.45, 0.01)]
)
def test_matches_options_strategy_advisor(right, S, K, T, sigma, q):
    pytest.importorskip("scipy")
    sys.path.insert(0, str(SKILL_SCRIPTS))
    from black_scholes import OptionPricer

    ref = OptionPricer(S=S, K=K, T=T, r=0.04, sigma=sigma, q=q).get_all_greeks(right)
    got = pricing.greeks(right, S, K, T, 0.04, sigma, q)
    for name in ("price", "delta", "gamma", "theta", "vega", "rho"):
        assert getattr(got, name) == pytest.approx(float(ref[name]), rel=1e-9, abs=1e-12)


def test_put_call_parity():
    c = pricing.greeks("call", 100, 95, 0.5, 0.05, 0.25).price
    p = pricing.greeks("put", 100, 95, 0.5, 0.05, 0.25).price
    assert c - p == pytest.approx(100 - 95 * pytest.importorskip("math").exp(-0.05 * 0.5))


def test_expired_option_is_intrinsic():
    g = pricing.greeks("put", 90, 100, 0, 0.04, 0.3)
    assert g.price == 10 and g.delta == -1 and g.theta == 0


def test_historical_volatility_flat_and_short():
    assert pricing.historical_volatility([10, 10, 10, 10]) == 0
    assert pricing.historical_volatility([10]) is None


# ---- strategies ----


def test_long_call_numbers():
    a = analyze(inputs())
    assert a.max_profit is None  # unlimited
    assert a.max_loss == pytest.approx(300)
    assert a.breakevens == [103.0]
    assert a.capital == pytest.approx(300)
    assert a.position_greeks["theta"] < 0 < a.position_greeks["delta"]
    assert 0 < a.prob_profit < 0.5


def test_long_put_numbers():
    a = analyze(inputs(strategy="long_put", premium=2.5, contracts=2))
    assert a.max_loss == pytest.approx(500)
    assert a.max_profit == pytest.approx((100 - 2.5) * 200)
    assert a.breakevens == [97.5]


def test_cash_secured_put():
    a = analyze(inputs(strategy="cash_secured_put", strike=15, price=16, premium=0.40))
    assert a.net_premium == pytest.approx(-0.40)
    assert a.max_profit == pytest.approx(40)
    assert a.max_loss == pytest.approx((15 - 0.40) * 100)
    assert a.capital == pytest.approx(1500)
    assert a.breakevens == [14.6]
    assert a.position_greeks["theta"] > 0


def test_covered_call_uses_cost_basis():
    a = analyze(inputs(strategy="covered_call", strike=110, premium=2, cost_basis=95))
    assert a.max_profit == pytest.approx((110 - 95 + 2) * 100)
    assert a.max_loss == pytest.approx((95 - 2) * 100)
    assert a.breakevens == [93.0]
    assert a.capital == pytest.approx(9500)


def test_bull_call_spread_is_capped_and_flagged_level3():
    a = analyze(
        inputs(strategy="bull_call_spread", strike=100, strike2=110, premium=4, premium2=1.5)
    )
    assert a.max_loss == pytest.approx(250)
    assert a.max_profit == pytest.approx(750)
    assert a.breakevens == [102.5]
    assert any("Level 3" in w for w in a.warnings)


def test_bear_put_spread_strikes_must_be_ordered():
    with pytest.raises(ValueError, match="below"):
        analyze(inputs(strategy="bear_put_spread", strike=100, strike2=105))


def test_blank_premium_is_estimated():
    a = analyze(inputs(premium=None))
    assert a.legs[0].estimated
    assert a.legs[0].premium == pytest.approx(a.legs[0].greeks.price, abs=0.005)
    assert any("estimates" in w for w in a.warnings)


def test_risk_warning_against_account():
    a = analyze(inputs(account=2000))
    assert a.risk_pct == pytest.approx(15)
    assert any("1-2%" in w for w in a.warnings)


def test_past_expiry_rejected():
    with pytest.raises(ValueError, match="past"):
        analyze(inputs(expiry=date(2026, 11, 1)))


# ---- page ----


@pytest.fixture
def app(reports_dir, state_dir, tmp_path):
    jdir = tmp_path / "journal"
    jdir.mkdir()
    a = create_app(
        {"REPORTS_DIR": reports_dir, "STATE_DIR": state_dir, "ALPACA": None, "JOURNAL_DIR": jdir}
    )
    a.testing = True
    return a


def test_blank_page_renders(app):
    body = app.test_client().get("/options/").get_data(as_text=True)
    assert "Options calculator" in body and "Bull call spread (Level 3)" in body
    assert "Max profit" not in body


def test_calculation_renders(app):
    body = (
        app.test_client()
        .get(
            "/options/?go=1&strategy=long_call&price=100&strike=100&premium=3&iv=30&expiry=2099-01-16"
        )
        .get_data(as_text=True)
    )
    assert "Unlimited" in body and "$300.00" in body and "<svg" in body
    assert "Level 2 OK" in body


def test_bad_input_shows_error(app):
    body = (
        app.test_client()
        .get("/options/?go=1&strategy=long_call&price=abc&strike=100&iv=30")
        .get_data(as_text=True)
    )
    assert "isn&#39;t a number" in body


def test_ticker_without_alpaca_asks_for_price(app):
    body = app.test_client().get("/options/?symbol=aapl").get_data(as_text=True)
    assert "type the stock price yourself" in body


def test_journal_option_trade_links_to_calculator(app):
    client = app.test_client()
    tid = new_trade(
        client,
        asset_class="option",
        symbol="aaa",
        expiry="2026-12-19",
        strike="25",
        right="put",
        side="sell",
        quantity="1",
        price="0.80",
        planned_stop="",
    )
    page = client.get(f"/journal/{tid}").get_data(as_text=True)
    assert "Open in Options calculator" in page
    assert "strategy=cash_secured_put" in page and "premium=0.80" in page
    stock = new_trade(client)
    assert "Open in Options calculator" not in client.get(f"/journal/{stock}").get_data(
        as_text=True
    )


# ---- Alpaca market data ----


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_stock_quote_is_get_only_and_cached(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req.get_method(), req.full_url))
        if "/trades/latest" in req.full_url:
            return FakeResponse({"trade": {"p": 101.5, "t": "2026-11-20T15:00:00Z"}})
        return FakeResponse({"bars": [{"c": 100}, {"c": 101}, {"c": 99}, {"c": 102}]})

    monkeypatch.setattr(alpaca, "urlopen", fake_urlopen)
    client = alpaca.AlpacaReadOnly("k", "s")
    q = client.stock_quote("aapl")
    client.stock_quote("AAPL")
    assert q["price"] == 101.5 and q["closes"] == [100, 101, 99, 102]
    assert len(calls) == 2 and all(m == "GET" for m, _ in calls)
    assert all(u.startswith("https://data.alpaca.markets/v2/stocks/AAPL/") for _, u in calls)
    assert "feed=iex" in calls[0][1] and "feed=sip" in calls[1][1]


def test_stock_quote_rejects_bad_symbol():
    with pytest.raises(alpaca.AlpacaError):
        alpaca.AlpacaReadOnly("k", "s").stock_quote("../v2/account")

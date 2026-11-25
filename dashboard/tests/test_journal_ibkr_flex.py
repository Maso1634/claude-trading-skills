from decimal import Decimal as D

import pytest
from app.journal import ibkr_flex

# Shape of an IBKR Activity Flex Query (Trades section, Execution level), XML.
# Every value below is made up.
SAMPLE = """<FlexQueryResponse queryName="dashboard-trades" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="DU0000000" fromDate="20261101" toDate="20261125">
<Trades>
<Trade assetCategory="STK" symbol="BBB" buySell="SELL" quantity="-5" tradePrice="12" ibCommission="-1" currency="USD"
  dateTime="20261110;153000" tradeID="102" ibExecID="0001.02" levelOfDetail="EXECUTION" transactionType="ExchTrade" />
<Trade assetCategory="STK" symbol="BBB" buySell="BUY" quantity="10" tradePrice="10" ibCommission="-1" currency="USD"
  dateTime="20261105;093512" tradeID="101" ibExecID="0001.01" levelOfDetail="EXECUTION" transactionType="ExchTrade" />
<Trade assetCategory="OPT" symbol="AAA   261114C00020000" underlyingSymbol="AAA" expiry="20261114" strike="20"
  putCall="C" multiplier="100" buySell="SELL" quantity="-1" tradePrice="0.8" ibCommission="-0.65" currency="USD"
  dateTime="20261112;101500" tradeID="103" ibExecID="0001.03" levelOfDetail="EXECUTION" transactionType="ExchTrade" />
<Trade assetCategory="OPT" symbol="AAA   261114C00020000" underlyingSymbol="AAA" expiry="20261114" strike="20"
  putCall="C" multiplier="100" buySell="BUY" quantity="1" tradePrice="0" ibCommission="0" currency="USD"
  dateTime="20261114;162000" tradeID="104" ibExecID="" notes="Ep" levelOfDetail="EXECUTION" transactionType="BookTrade" />
<Trade assetCategory="CASH" symbol="EUR.USD" buySell="BUY" quantity="100" tradePrice="1.1" ibCommission="-2"
  dateTime="20261111;120000" tradeID="105" levelOfDetail="EXECUTION" />
<Trade assetCategory="STK" symbol="BBB" buySell="BUY" quantity="10" tradePrice="10" ibCommission="-1"
  dateTime="20261105;093512" tradeID="101" levelOfDetail="ORDER" />
</Trades>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""


def test_parse_sample():
    res = ibkr_flex.parse(SAMPLE.encode())
    assert [(f.symbol, f.side) for f in res.fills] == [
        ("BBB", "buy"),
        ("BBB", "sell"),
        ("AAA 2026-11-14 20 C", "sell"),
        ("AAA 2026-11-14 20 C", "buy"),
    ]
    buy, sell, sto, expired = res.fills
    assert buy.quantity == D("10") and buy.price == D("10") and buy.fee == D("1")
    assert buy.executed_at == "2026-11-05T14:35:12Z"  # 9:35 AM Eastern
    assert sell.quantity == D("5")  # IBKR signs quantity; journal uses side + positive qty
    assert sto.asset_class == "option" and sto.underlying == "AAA"
    assert sto.expiry == "2026-11-14" and sto.strike == D("20") and sto.right == "call"
    assert sto.multiplier == D("100") and sto.fee == D("0.65")
    assert expired.price == D("0") and expired.note == "expired"
    assert res.skipped == {"CASH": 1, "ORDER rows": 1}
    assert res.errors == []
    assert len({f.source_key for f in res.fills}) == 4
    assert buy.source_key == "ibkr:101"


def test_not_a_flex_file():
    with pytest.raises(ibkr_flex.NotFlexXML):
        ibkr_flex.parse(b"Activity Date,Instrument\n")
    with pytest.raises(ibkr_flex.NotFlexXML, match="Trades"):
        ibkr_flex.parse(b"<FlexQueryResponse><FlexStatements/></FlexQueryResponse>")


def test_flex_error_response_is_explained():
    err = b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode><ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>"
    with pytest.raises(ibkr_flex.FlexError, match="Token has expired"):
        ibkr_flex.parse(err)


def test_fetch_requests_then_polls_until_ready():
    calls = []
    replies = [
        b"<FlexStatementResponse><Status>Success</Status><ReferenceCode>555</ReferenceCode>"
        b"<Url>https://example.invalid/GetStatement</Url></FlexStatementResponse>",
        b"<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode>"
        b"<ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage></FlexStatementResponse>",
        SAMPLE.encode(),
    ]

    def get(url):
        calls.append(url)
        return replies[len(calls) - 1]

    data = ibkr_flex.fetch("TOKEN", "42", http_get=get, sleep=lambda s: None)
    assert data == SAMPLE.encode()
    assert "SendRequest?t=TOKEN&q=42&v=3" in calls[0]
    assert calls[1] == calls[2] == "https://example.invalid/GetStatement?t=TOKEN&q=555&v=3"


def test_fetch_failure_raises_flex_error():
    bad = b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1015</ErrorCode><ErrorMessage>Token is invalid.</ErrorMessage></FlexStatementResponse>"
    with pytest.raises(ibkr_flex.FlexError, match="Token is invalid"):
        ibkr_flex.fetch("T", "1", http_get=lambda url: bad, sleep=lambda s: None)


def test_settings_from_env():
    assert ibkr_flex.settings_from_env({}) is None
    assert ibkr_flex.settings_from_env({"IBKR_FLEX_TOKEN": "t"}) is None
    s = ibkr_flex.settings_from_env({"IBKR_FLEX_TOKEN": " t ", "IBKR_FLEX_QUERY_ID": "42"})
    assert s == ("t", "42")

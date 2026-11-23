import json
from datetime import datetime, timedelta, timezone

import httpx

from stock_terminal.clients.edgar_client import NO_AGENT, EdgarClient, describe_8k, describe_form4, parse_form4
from stock_terminal.config import Keys
from stock_terminal.datapoint import FRESH, MISSING
from stock_terminal.news.models import KIND_FILING

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)

FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>COOK TIMOTHY D</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-11-18</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>350</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-11-18</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>352</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def test_describe_8k_names_items_and_picks_catalyst():
    assert describe_8k("2.02,9.01") == ("8-K: results of operations (earnings)", "earnings")
    assert describe_8k("9.01") == ("8-K: financial statements and exhibits", "other")
    assert describe_8k("5.02,7.01") == ("8-K: director or officer change; Regulation FD disclosure", "other")
    assert describe_8k("2.01") == ("8-K: acquisition or disposal of assets completed", "mna")
    assert describe_8k("") == ("8-K: no items listed", "other")


def test_form4_parse_and_headline():
    parsed = parse_form4(FORM4_XML)
    assert parsed["owner"] == "COOK TIMOTHY D" and parsed["title"] == "Chief Executive Officer" and len(parsed["transactions"]) == 2
    assert describe_form4(parsed) == "Form 4: Cook Timothy D (Chief Executive Officer) sold 2,000 shares at $351.00"
    assert describe_form4({"owner": "X", "title": "Director", "transactions": [], "derivative_count": 2}) == "Form 4: X (Director) reported derivative (option) transactions only"
    gift = {"owner": "LEVINSON ARTHUR D", "title": "Director", "transactions": [{"date": "", "code": "G", "shares": 50000.0, "price": 0.0, "ad": "D"}], "derivative_count": 0}
    assert describe_form4(gift) == "Form 4: Levinson Arthur D (Director) gifted 50,000 shares"   # seen live 2026-11-22: a $0 gift price is not a price


def submissions(now: datetime) -> dict:
    recent_8k = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    recent_4 = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    old = (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "cik": "320193",
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-26-000100", "0000320193-26-000101", "0000320193-26-000102", "0000320193-26-000050"],
                "filingDate": ["2026-11-18", "2026-11-19", "2026-11-19", "2026-08-20"],
                "acceptanceDateTime": [recent_8k, recent_4, recent_4, old],
                "form": ["8-K", "4", "10-Q", "8-K"],
                "items": ["2.02,9.01", "", "", "8.01"],
                "primaryDocument": ["aapl-8k.htm", "xslF345X05/wk-form4_1.xml", "aapl-10q.htm", "old-8k.htm"],
                "primaryDocDescription": ["8-K", "FORM 4", "10-Q", "8-K"],
            }
        },
    }


def make_client(cfg, cache, agent="stock-terminal tyler@example.org"):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers.get("User-Agent") == agent
        url = str(request.url)
        if url.endswith("company_tickers.json"):
            return httpx.Response(200, content=json.dumps({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}))
        if "submissions/CIK0000320193.json" in url:
            return httpx.Response(200, content=json.dumps(submissions(NOW)))
        if url.endswith("/000032019326000101/wk-form4_1.xml"):
            return httpx.Response(200, text=FORM4_XML)
        return httpx.Response(404, text=url)

    return EdgarClient(Keys(edgar_user_agent=agent), cache, cfg, transport=httpx.MockTransport(handler)), seen


def test_filings_become_items(cfg, cache):
    client, seen = make_client(cfg, cache)
    items, dp = client.filings("aapl", NOW - timedelta(days=30))
    assert dp.status == FRESH and len(items) == 2
    eight_k, form4 = items
    assert eight_k.id == "edgar:0000320193-26-000100" and eight_k.kind == KIND_FILING and eight_k.catalyst == "earnings"
    assert eight_k.headline == "8-K: results of operations (earnings)" and eight_k.symbols == ["AAPL"] and eight_k.source == "edgar/8-K"
    assert eight_k.url == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000100/aapl-8k.htm"
    assert form4.headline == "Form 4: Cook Timothy D (Chief Executive Officer) sold 2,000 shares at $351.00" and form4.catalyst == "insider"
    assert form4.url.endswith("/000032019326000101/xslF345X05/wk-form4_1.xml")
    assert len(seen) == 3                                             # tickers table, submissions, one Form 4 XML
    client.filings("AAPL", NOW - timedelta(days=30))
    assert len(seen) == 3                                             # everything cached


def test_unknown_ticker_and_missing_agent(cfg, cache):
    client, _ = make_client(cfg, cache)
    items, dp = client.filings("ZZZZ", NOW)
    assert items == [] and dp.status == MISSING and "not in the SEC's ticker list" in dp.note
    bare = EdgarClient(Keys(edgar_user_agent="stock-terminal your.email@example.com"), cache, cfg)
    items, dp = bare.filings("AAPL", NOW)
    assert items == [] and dp.status == MISSING and dp.note == NO_AGENT   # the placeholder from .env.example does not count

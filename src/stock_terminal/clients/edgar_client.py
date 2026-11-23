"""SEC EDGAR: 8-K filings and Form 4 insider transactions.

Facts this code relies on (verified 2026-11-15): no key; at most 10 requests a second;
every request must carry a User-Agent naming the app and a contact email (that is the
EDGAR_USER_AGENT line in .env); `company_tickers.json` maps a ticker to its CIK (the
SEC's company number) and `data.sec.gov/submissions/CIK##########.json` lists the
company's recent filings, Form 4s included. A Form 4's details live in its XML, which
is fetched once per filing and parsed into "who bought or sold how many shares at
what price".
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Optional

import httpx

from ..cache import Cache
from ..config import Keys
from ..datapoint import DataPoint
from ..news.models import KIND_FILING, NewsItem, parse_when
from ..ratelimit import RateLimiter
from .base import BaseClient

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{doc}"
NO_AGENT = "no EDGAR_USER_AGENT (app name + your email) in .env"
FORMS = ("8-K", "8-K/A", "4", "4/A")
MAX_FORM4_DETAILS = 10   # newest Form 4s whose XML is fetched per ticker per refresh

# 8-K item numbers in plain words (the SEC's own titles, shortened).
ITEM_NAMES = {
    "1.01": "material agreement signed",
    "1.02": "material agreement ended",
    "1.03": "bankruptcy or receivership",
    "1.05": "material cybersecurity incident",
    "2.01": "acquisition or disposal of assets completed",
    "2.02": "results of operations (earnings)",
    "2.03": "new financial obligation",
    "2.04": "triggering event on an obligation",
    "2.05": "exit or disposal costs",
    "2.06": "material impairment",
    "3.01": "delisting notice",
    "3.02": "unregistered sale of shares",
    "3.03": "change to security holders' rights",
    "4.01": "change of auditor",
    "4.02": "earlier financials not to be relied on",
    "5.01": "change in control",
    "5.02": "director or officer change",
    "5.03": "bylaws or charter amended",
    "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure",
    "8.01": "other event",
    "9.01": "financial statements and exhibits",
}
ITEM_CATALYST = {"2.02": "earnings", "2.01": "mna", "1.03": "legal", "3.01": "legal", "4.01": "legal", "4.02": "legal"}
CODE_WORDS = {
    "P": "bought",
    "S": "sold",
    "A": "was awarded",
    "M": "exercised options for",
    "F": "had shares withheld for taxes:",
    "G": "gifted",
    "D": "disposed of",
    "C": "converted",
}


def item_codes(items: Any) -> list[str]:
    return [i.strip() for i in str(items or "").split(",") if i.strip()]


def describe_8k(items: Any) -> tuple[str, str]:
    """Headline text and catalyst for an 8-K from its item list (for example '2.02,9.01')."""
    codes = item_codes(items)
    named = [ITEM_NAMES.get(c, f"item {c}") for c in codes if c != "9.01"] or [ITEM_NAMES.get(c, f"item {c}") for c in codes]
    text = "; ".join(named) if named else "no items listed"
    catalyst = next((ITEM_CATALYST[c] for c in codes if c in ITEM_CATALYST), "other")
    return f"8-K: {text}", catalyst


def parse_form4(xml_text: str) -> dict[str, Any]:
    """Owner, title and the non-derivative transactions from a Form 4's XML."""
    root = ET.fromstring(xml_text)
    owner = (root.findtext("reportingOwner/reportingOwnerId/rptOwnerName") or "insider").strip()
    rel = root.find("reportingOwner/reportingOwnerRelationship")
    title = ""
    if rel is not None:
        title = (rel.findtext("officerTitle") or "").strip()
        if not title and (rel.findtext("isDirector") or "").strip().lower() in ("1", "true"):
            title = "Director"
        if not title and (rel.findtext("isTenPercentOwner") or "").strip().lower() in ("1", "true"):
            title = "10% owner"
    transactions = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        shares_text = tx.findtext("transactionAmounts/transactionShares/value")
        price_text = tx.findtext("transactionAmounts/transactionPricePerShare/value")
        try:
            shares = float(shares_text) if shares_text else 0.0
        except ValueError:
            shares = 0.0
        try:
            price = float(price_text) if price_text else None
        except ValueError:
            price = None
        transactions.append(
            {
                "date": (tx.findtext("transactionDate/value") or "").strip(),
                "code": (tx.findtext("transactionCoding/transactionCode") or "?").strip().upper(),
                "shares": shares,
                "price": price,
                "ad": (tx.findtext("transactionAmounts/transactionAcquiredDisposedCode/value") or "").strip().upper(),
            }
        )
    derivative = len(root.findall("derivativeTable/derivativeTransaction"))
    return {"owner": owner, "title": title, "transactions": transactions, "derivative_count": derivative}


def describe_form4(parsed: dict[str, Any]) -> str:
    owner = str(parsed.get("owner") or "insider").title()
    title = str(parsed.get("title") or "").strip()
    who = f"{owner} ({title})" if title else owner
    by_code: dict[str, dict[str, float]] = {}
    for tx in parsed.get("transactions") or []:
        bucket = by_code.setdefault(tx["code"], {"shares": 0.0, "value": 0.0, "priced": 0.0})
        bucket["shares"] += tx["shares"]
        if tx.get("price") is not None:
            bucket["value"] += tx["shares"] * tx["price"]
            bucket["priced"] += tx["shares"]
    parts = []
    for code, bucket in by_code.items():
        words = CODE_WORDS.get(code, f"transaction code {code}:")
        text = f"{words} {bucket['shares']:,.0f} shares"
        if bucket["priced"] > 0 and bucket["value"] > 0:   # gifts and awards carry a $0 price: say nothing rather than "$0.00"
            text += f" at ${bucket['value'] / bucket['priced']:,.2f}"
        parts.append(text)
    if not parts:
        if parsed.get("derivative_count"):
            return f"Form 4: {who} reported derivative (option) transactions only"
        return f"Form 4: {who} filed with no transactions listed"
    return f"Form 4: {who} " + "; ".join(parts)


def _folder(accession: str) -> str:
    return accession.replace("-", "")


class EdgarClient(BaseClient):
    name = "edgar"

    def __init__(
        self,
        keys: Keys,
        cache: Cache,
        cfg: dict[str, Any],
        logger: Optional[logging.Logger] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        super().__init__(cache, RateLimiter(int(cfg["edgar"]["calls_per_second"]), 1.0), logger)
        self.ttl_cik = cfg["cache"]["ttl_seconds"]["cik"]
        self.ttl_filings = cfg["cache"]["ttl_seconds"]["edgar"]
        self.ttl_form4 = 30 * 86400
        self.enabled = keys.has_edgar
        headers = {"User-Agent": keys.edgar_user_agent, "Accept-Encoding": "gzip, deflate"} if keys.edgar_user_agent else {}
        self._http = httpx.Client(timeout=15.0, transport=transport, headers=headers, follow_redirects=True)

    def _get_json(self, url: str) -> Any:
        response = self._http.get(url)
        response.raise_for_status()
        return response.json()

    def _get_text(self, url: str) -> str:
        response = self._http.get(url)
        response.raise_for_status()
        return response.text

    # ----- ticker -> CIK -----
    def cik_for(self, ticker: str) -> DataPoint:
        ticker = ticker.upper()
        source = "edgar/company_tickers"
        if not self.enabled:
            return DataPoint.missing(source, NO_AGENT)
        table = self.fetch_cached("edgar:tickers", self.ttl_cik, self._fetch_ticker_table, source)
        if not table.ok:
            return table
        cik = table.value.get(ticker)
        if cik is None:
            return DataPoint.missing(source, f"{ticker} not in the SEC's ticker list")
        return DataPoint(int(cik), source, table.status, fetched_at=table.fetched_at, note=table.note)

    def _fetch_ticker_table(self) -> dict[str, int]:
        raw = self._get_json(TICKERS_URL)
        rows = raw.values() if isinstance(raw, dict) else raw
        return {str(r["ticker"]).upper(): int(r["cik_str"]) for r in rows if r.get("ticker")}

    # ----- filings -----
    def filings(self, ticker: str, since: datetime) -> tuple[list[NewsItem], DataPoint]:
        ticker = ticker.upper()
        source = "edgar/submissions"
        if not self.enabled:
            return [], DataPoint.missing(source, NO_AGENT)
        cik_dp = self.cik_for(ticker)
        if not cik_dp.ok:
            return [], DataPoint.missing(source, cik_dp.note)
        cik = int(cik_dp.value)
        dp = self.fetch_cached(f"edgar:filings:{cik}", self.ttl_filings, lambda: self._fetch_filings(cik), source)
        if not dp.ok:
            return [], dp
        items: list[NewsItem] = []
        form4_budget = MAX_FORM4_DETAILS
        for filing in dp.value:
            published = parse_when(filing.get("acceptanceDateTime")) or parse_when(filing.get("filingDate"))
            if published is None or published < since:
                continue
            form = filing["form"]
            folder = _folder(filing["accessionNumber"])
            doc = filing.get("primaryDocument") or ""
            url = ARCHIVE_URL.format(cik=cik, folder=folder, doc=doc) if doc else ""
            if form.startswith("8-K"):
                headline, catalyst = describe_8k(filing.get("items"))
                summary = filing.get("primaryDocDescription") or ""
            else:
                catalyst = "insider"
                headline, summary = f"Form 4: insider filing ({filing['accessionNumber']})", ""
                if form4_budget > 0 and doc:
                    form4_budget -= 1
                    detail = self.fetch_cached(
                        f"edgar:form4:{folder}", self.ttl_form4, lambda u=self._xml_url(cik, folder, doc): parse_form4(self._get_text(u)), "edgar/form4"
                    )
                    if detail.ok:
                        headline = describe_form4(detail.value)
                    else:
                        summary = f"details unavailable: {detail.note}"
            items.append(
                NewsItem(
                    id=f"edgar:{filing['accessionNumber']}",
                    source=f"edgar/{'8-K' if form.startswith('8-K') else 'Form 4'}",
                    headline=headline,
                    published_at=published,
                    url=url,
                    summary=summary,
                    symbols=[ticker],
                    kind=KIND_FILING,
                    catalyst=catalyst,
                )
            )
        return items, dp

    @staticmethod
    def _xml_url(cik: int, folder: str, primary_doc: str) -> str:
        # EDGAR lists the rendered copy (xslF345X05/form4.xml); the raw XML is the same file name at the folder root.
        return ARCHIVE_URL.format(cik=cik, folder=folder, doc=primary_doc.split("/")[-1])

    def _fetch_filings(self, cik: int) -> list[dict[str, Any]]:
        raw = self._get_json(SUBMISSIONS_URL.format(cik=cik))
        recent = (raw.get("filings") or {}).get("recent") or {}
        columns = ("accessionNumber", "filingDate", "acceptanceDateTime", "form", "items", "primaryDocument", "primaryDocDescription")
        length = len(recent.get("form") or [])
        out = []
        for i in range(length):
            form = str(recent["form"][i])
            if form not in FORMS:
                continue
            out.append({col: (recent.get(col) or [None] * length)[i] for col in columns})
        return out

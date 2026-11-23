"""Alpaca market data: stock snapshots (latest trade and daily bars) and the market clock.

Free plan facts this code relies on (verified 2026-11-15): the stock feed is IEX, one
exchange, real-time; paper-trading keys serve market data; the clock comes from the
trading API in read-only form. Nothing here can place an order.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

from alpaca.data.enums import Adjustment, DataFeed, OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
from alpaca.trading.enums import ContractType
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient

from ..cache import Cache
from ..config import Keys
from ..datapoint import DELAYED, FRESH, DataPoint, human_age, utcnow
from ..ratelimit import RateLimiter
from .base import BaseClient

NO_KEY = "no Alpaca key in .env"


@dataclass
class Quote:
    symbol: str
    price: DataPoint        # last trade price
    change_pct: DataPoint   # percent change versus the previous session's close
    last_trade_at: Optional[datetime] = None
    prev_close: Optional[float] = None

    @classmethod
    def missing(cls, symbol: str, source: str, note: str) -> "Quote":
        return cls(symbol, DataPoint.missing(source, note), DataPoint.missing(source, note))


def _parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    return datetime.fromisoformat(text)


def quotes_from_payload(
    payload: Optional[dict[str, Any]],
    symbols: list[str],
    source: str,
    status: str,
    note: str,
    fetched_at: Optional[datetime],
    market_open: Optional[bool],
    delayed_after_seconds: float,
    now: Optional[datetime] = None,
) -> dict[str, Quote]:
    """Turn the cached JSON payload into one Quote per requested symbol. Pure, so it is tested directly."""
    now = now or utcnow()
    payload = payload or {}
    quotes: dict[str, Quote] = {}
    for symbol in symbols:
        raw = payload.get(symbol)
        if not raw or raw.get("price") is None:
            quotes[symbol] = Quote.missing(symbol, source, note or "no trade returned for this symbol")
            continue
        trade_at = _parse_iso(raw.get("trade_at"))
        price_status, price_note = status, note
        age = (now - trade_at).total_seconds() if trade_at else None
        if status == FRESH and market_open is True and age is not None and age > delayed_after_seconds:
            price_status, price_note = DELAYED, f"last trade {human_age(age)} ago while the market is open"
        price = DataPoint(float(raw["price"]), source, price_status, as_of=trade_at, fetched_at=fetched_at, note=price_note)
        prev_close = raw.get("prev_close")
        if prev_close:
            change = (float(raw["price"]) / float(prev_close) - 1.0) * 100.0
            change_dp = DataPoint(change, source, price_status, as_of=trade_at, fetched_at=fetched_at, note=price_note)
        else:
            change_dp = DataPoint.missing(source, "no previous close in the snapshot")
        quotes[symbol] = Quote(symbol, price, change_dp, trade_at, float(prev_close) if prev_close else None)
    return quotes


class AlpacaClient(BaseClient):
    name = "alpaca"

    def __init__(
        self,
        keys: Keys,
        cache: Cache,
        cfg: dict[str, Any],
        logger: Optional[logging.Logger] = None,
        data_client: Any = None,
        trading_client: Any = None,
        option_client: Any = None,
    ) -> None:
        super().__init__(cache, RateLimiter.per_minute(cfg["alpaca"]["calls_per_minute"]), logger)
        self.feed = DataFeed(cfg["alpaca"]["feed"])
        self.source = f"alpaca/{self.feed.value}"
        self.ttl_snapshot = cfg["cache"]["ttl_seconds"]["snapshot"]
        self.ttl_clock = cfg["cache"]["ttl_seconds"]["clock"]
        self.ttl_bars = cfg["cache"]["ttl_seconds"]["bars"]
        self.ttl_bars_daily = cfg["cache"]["ttl_seconds"]["bars_daily"]
        self.ttl_options = cfg["cache"]["ttl_seconds"]["options"]
        self.ttl_contracts = cfg["cache"]["ttl_seconds"]["contracts"]
        self.delayed_after = cfg["alpaca"]["delayed_after_seconds"]
        self.enabled = keys.has_alpaca
        if data_client is not None or trading_client is not None or option_client is not None:
            # Tests inject fakes; a fake counts as enabled.
            self.enabled = True
        self._data = data_client
        self._trading = trading_client
        self._options = option_client
        if self.enabled and option_client is None and keys.has_alpaca:
            self._options = OptionHistoricalDataClient(keys.alpaca_key_id, keys.alpaca_secret)
        if self.enabled and data_client is None and keys.has_alpaca:
            self._data = StockHistoricalDataClient(keys.alpaca_key_id, keys.alpaca_secret)
        if self.enabled and trading_client is None and keys.has_alpaca:
            # Read-only use: get_clock(). No order method is ever called.
            self._trading = TradingClient(keys.alpaca_key_id, keys.alpaca_secret, paper=True)

    # ----- snapshots -----
    def snapshots(self, symbols: list[str], market_open: Optional[bool] = None) -> dict[str, Quote]:
        wanted = sorted({s.upper() for s in symbols if s})
        if not wanted:
            return {}
        if not self.enabled:
            return {s: Quote.missing(s, self.source, NO_KEY) for s in wanted}
        key = "alpaca:snapshot:" + ",".join(wanted)
        dp = self.fetch_cached(key, self.ttl_snapshot, lambda: self._fetch_snapshots(wanted), self.source)
        return quotes_from_payload(
            dp.value, wanted, self.source, dp.status, dp.note, dp.fetched_at, market_open, self.delayed_after
        )

    def _fetch_snapshots(self, symbols: list[str]) -> dict[str, Any]:
        request = StockSnapshotRequest(symbol_or_symbols=list(symbols), feed=self.feed)
        snaps = self._data.get_stock_snapshot(request)
        out: dict[str, Any] = {}
        for symbol in symbols:
            snap = snaps.get(symbol) if hasattr(snaps, "get") else None
            if snap is None:
                out[symbol] = None
                continue
            trade = getattr(snap, "latest_trade", None)
            daily = getattr(snap, "daily_bar", None)
            prev = getattr(snap, "previous_daily_bar", None)
            out[symbol] = {
                "price": float(trade.price) if trade is not None else None,
                "trade_at": trade.timestamp.isoformat() if trade is not None else None,
                "daily_close": float(daily.close) if daily is not None else None,
                "daily_at": daily.timestamp.isoformat() if daily is not None else None,
                "prev_close": float(prev.close) if prev is not None else None,
            }
        return out

    # ----- market clock -----
    def clock(self) -> DataPoint:
        if not self.enabled:
            return DataPoint.missing("alpaca/clock", NO_KEY)
        return self.fetch_cached("alpaca:clock", self.ttl_clock, self._fetch_clock, "alpaca/clock")

    def _fetch_clock(self) -> dict[str, Any]:
        clock = self._trading.get_clock()
        return {
            "is_open": bool(clock.is_open),
            "timestamp": clock.timestamp.isoformat(),
            "next_open": clock.next_open.isoformat(),
            "next_close": clock.next_close.isoformat(),
        }

    # ----- bars (for the news reaction tracker) -----
    def bars(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> DataPoint:
        """Minute or daily bars as a list of {"t": iso time, "c": close}. `timeframe` is "minute" or "day"."""
        symbol = symbol.upper()
        source = f"{self.source}/bars"
        if not self.enabled:
            return DataPoint.missing(source, NO_KEY)
        if timeframe not in ("minute", "day"):
            raise ValueError("timeframe must be minute or day")
        end = min(end, utcnow() - timedelta(minutes=1))
        if end <= start:
            return DataPoint([], source, FRESH, note="window is in the future")
        key = f"alpaca:bars:{symbol}:{timeframe}:{start.strftime('%Y%m%dT%H%M')}:{end.strftime('%Y%m%dT%H%M')}"
        return self.fetch_cached(key, self.ttl_bars, lambda: self._fetch_bars(symbol, timeframe, start, end), source)

    def _fetch_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute if timeframe == "minute" else TimeFrame.Day,
            start=start,
            end=end,
            feed=self.feed,
            limit=10_000,
        )
        result = self._data.get_stock_bars(request)
        data = getattr(result, "data", result)
        rows = data.get(symbol, []) if hasattr(data, "get") else []
        return [{"t": bar.timestamp.isoformat(), "c": float(bar.close)} for bar in rows]

    # ----- daily closes for many symbols (the score's universe) -----
    def daily_closes(self, symbols: list[str], start: datetime, end: datetime) -> DataPoint:
        """Split-adjusted daily closes: {symbol: [{"t": date, "c": close}]}. Requests of 25 symbols; alpaca-py pages each."""
        wanted = sorted({s.upper() for s in symbols if s})
        source = f"{self.source}/daily"
        if not wanted:
            return DataPoint({}, source, FRESH)
        if not self.enabled:
            return DataPoint.missing(source, NO_KEY)
        end = min(end, utcnow() - timedelta(minutes=16))
        key = f"alpaca:daily:{hash(tuple(wanted)) & 0xFFFFFFFF:08x}:{len(wanted)}:{start.date()}:{end.strftime('%Y%m%dT%H%M')}"
        return self.fetch_cached(key, self.ttl_bars_daily, lambda: self._fetch_daily(wanted, start, end), source)

    def _fetch_daily(self, symbols: list[str], start: datetime, end: datetime) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
        # No `limit`: alpaca-py treats it as the total bar count across the whole basket and stops paging there, which
        # on the first live run (2026-11-22) cut 102 symbols x 275 days off at 10,000 bars, the letter F. Chunks of 25
        # keep each response small; the SDK pages through each chunk on its own.
        for i in range(0, len(symbols), 25):
            chunk = symbols[i : i + 25]
            request = StockBarsRequest(
                symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start, end=end,
                feed=self.feed, adjustment=Adjustment.SPLIT,
            )
            result = self._data.get_stock_bars(request)
            data = getattr(result, "data", result)
            for symbol in chunk:
                rows = data.get(symbol, []) if hasattr(data, "get") else []
                out[symbol] = [{"t": bar.timestamp.date().isoformat(), "c": float(bar.close)} for bar in rows]
        return out

    # ----- options: the contract list (read-only GET on the trading API) and the chain snapshots -----
    def option_contracts(self, symbol: str, expiry_from: date, expiry_to: date, kind: str, strike_lo: float, strike_hi: float) -> DataPoint:
        """Contracts with open interest, strike and expiry. Value: a list of dicts. Nothing here can place an order."""
        symbol = symbol.upper()
        source = "alpaca/option-contracts"
        if not self.enabled or self._trading is None:
            return DataPoint.missing(source, NO_KEY if not self.enabled else "no trading client for the contract list")
        key = f"alpaca:contracts:{symbol}:{kind}:{expiry_from}:{expiry_to}:{strike_lo:.0f}:{strike_hi:.0f}"
        return self.fetch_cached(key, self.ttl_contracts, lambda: self._fetch_contracts(symbol, expiry_from, expiry_to, kind, strike_lo, strike_hi), source)

    def _fetch_contracts(self, symbol: str, expiry_from: date, expiry_to: date, kind: str, strike_lo: float, strike_hi: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token = None
        for _ in range(20):   # pages of up to 1,000 contracts
            request = GetOptionContractsRequest(
                underlying_symbols=[symbol], expiration_date_gte=expiry_from, expiration_date_lte=expiry_to,
                type=ContractType.CALL if kind == "call" else ContractType.PUT,
                strike_price_gte=f"{strike_lo:.2f}", strike_price_lte=f"{strike_hi:.2f}", limit=1000, page_token=token,
            )
            result = self._trading.get_option_contracts(request)
            contracts = getattr(result, "option_contracts", None)
            if contracts is None:
                contracts = result if isinstance(result, list) else []
            for c in contracts:
                out.append({
                    "symbol": c.symbol, "expiration_date": c.expiration_date.isoformat(), "strike_price": float(c.strike_price),
                    "type": getattr(c.type, "value", str(c.type)), "open_interest": c.open_interest,
                    "open_interest_date": c.open_interest_date.isoformat() if c.open_interest_date else None,
                    "close_price": c.close_price,
                })
            token = getattr(result, "next_page_token", None)
            if not token:
                break
        return out

    def option_chain(self, symbol: str, expiry_from: date, expiry_to: date, kind: str, strike_lo: float, strike_hi: float) -> DataPoint:
        """Snapshots for the same filter: quote, implied volatility and Greeks per contract symbol, from the indicative feed."""
        symbol = symbol.upper()
        source = "alpaca/option-chain"
        if not self.enabled or self._options is None:
            return DataPoint.missing(source, NO_KEY if not self.enabled else "no option data client")
        key = f"alpaca:chain:{symbol}:{kind}:{expiry_from}:{expiry_to}:{strike_lo:.0f}:{strike_hi:.0f}"
        return self.fetch_cached(key, self.ttl_options, lambda: self._fetch_chain(symbol, expiry_from, expiry_to, kind, strike_lo, strike_hi), source)

    def _fetch_chain(self, symbol: str, expiry_from: date, expiry_to: date, kind: str, strike_lo: float, strike_hi: float) -> dict[str, dict[str, Any]]:
        request = OptionChainRequest(
            underlying_symbol=symbol, feed=OptionsFeed.INDICATIVE, type=ContractType.CALL if kind == "call" else ContractType.PUT,
            expiration_date_gte=expiry_from, expiration_date_lte=expiry_to, strike_price_gte=strike_lo, strike_price_lte=strike_hi,
        )
        chain = self._options.get_option_chain(request)
        out: dict[str, dict[str, Any]] = {}
        for contract_symbol, snap in (chain.items() if hasattr(chain, "items") else []):
            quote = getattr(snap, "latest_quote", None)
            greeks = getattr(snap, "greeks", None)
            out[contract_symbol] = {
                "bid": float(quote.bid_price) if quote is not None and quote.bid_price is not None else None,
                "ask": float(quote.ask_price) if quote is not None and quote.ask_price is not None else None,
                "quote_at": quote.timestamp.isoformat() if quote is not None and getattr(quote, "timestamp", None) else None,
                "iv": float(snap.implied_volatility) if getattr(snap, "implied_volatility", None) is not None else None,
                "greeks": {k: getattr(greeks, k, None) for k in ("delta", "gamma", "theta", "vega", "rho")} if greeks is not None else {},
            }
        return out

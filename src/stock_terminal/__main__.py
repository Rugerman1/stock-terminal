"""Entry point.

    python -m stock_terminal            run the screen
    python -m stock_terminal --check    fetch each source once, print every status, exit

Paths in config.yaml are relative to the folder you run from, so run from the repo root.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Optional

from .app import StockTerminalApp
from .cache import Cache
from .clients.fred_client import ATTRIBUTION
from .config import Keys, load_config, load_keys
from .logging_setup import setup_logging
from .provider import LiveProvider, Provider
from .watchlist import Watchlist


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock_terminal",
        description="Read-only stock news, quant read and validity terminal. Places no orders.",
    )
    parser.add_argument("--check", action="store_true", help="fetch each source once, print its status, and exit")
    parser.add_argument("--config", default="config.yaml", help="settings file (default: config.yaml)")
    parser.add_argument("--watchlist", default="watchlist.yaml", help="watchlist file (default: watchlist.yaml)")
    parser.add_argument("--env", default=".env", help="secrets file (default: .env)")
    return parser


def run_check(cfg: dict[str, Any], keys: Keys, provider: Provider, watchlist: Watchlist) -> int:
    print("Keys present (names only, values are never printed):")
    for name, present in keys.summary().items():
        print(f"  {name:<18} {'set' if present else 'unset'}")

    symbols = list(dict.fromkeys(list(cfg["top_strip"]["equities"]) + list(watchlist.tickers)))
    board = provider.refresh(symbols)

    clock = board.clock
    print("\nMarket clock:")
    if clock.ok:
        state = "OPEN" if board.market_open else "CLOSED"
        print(f"  {state}  next open {clock.value.get('next_open')}  next close {clock.value.get('next_close')}  [{clock.status.upper()}]")
    else:
        print(f"  [{clock.status.upper()}] {clock.note}")

    print("\nQuotes:")
    for symbol in symbols:
        quote = board.quotes[symbol]
        as_of = quote.last_trade_at.isoformat() if quote.last_trade_at else "?"
        print(
            f"  {symbol:<6} price {quote.price.display():>10}  chg {quote.change_pct.display('{:+.2f}%'):>9}"
            f"  trade at {as_of}  [{quote.price.status.upper()}] {quote.price.note}"
        )

    print("\nFRED:")
    for label, dp in board.fred.items():
        when = dp.as_of.date().isoformat() if dp.as_of else "?"
        print(f"  {label:<4} {dp.display():>8}  observation {when}  [{dp.status.upper()}] {dp.note}")

    print(f"\n{ATTRIBUTION}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    keys = load_keys(args.env)
    logger = setup_logging(cfg["log"]["path"])
    cache = Cache(cfg["cache"]["path"])
    provider = LiveProvider(cfg, keys, cache, logger)
    watchlist = Watchlist(args.watchlist)
    if args.check:
        logger.info("--check run; keys present: %s", keys)
        return run_check(cfg, keys, provider, watchlist)
    StockTerminalApp(cfg, watchlist, provider, logger=logger).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

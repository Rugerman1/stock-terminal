"""Entry point.

    python -m stock_terminal                 run the screen
    python -m stock_terminal --check         fetch each source once, print every status, exit
    python -m stock_terminal --news AAPL     print AAPL's tagged news feed, exit
    python -m stock_terminal --peers AAPL    print Finnhub's peers for AAPL as a relationships.yaml snippet
    python -m stock_terminal --reactions     print the average price move after headlines, by tag and catalyst
    python -m stock_terminal --score AAPL    print AAPL's long-term score with the full explain view
    python -m stock_terminal --options AAPL  print AAPL's call / put check with the full explain view
    python -m stock_terminal --universe      pull the universe's closes, fundamentals and news now (the nightly job)
    python -m stock_terminal --signals       print the signal log (label changes)
    python -m stock_terminal --rededupe      recompute the duplicate marks after a rule change

Paths in config.yaml are relative to the folder you run from, so run from the repo root.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Optional

from .app import StockTerminalApp, entry_markup
from .cache import Cache
from .clients.fred_client import ATTRIBUTION
from .config import Keys, load_config, load_keys
from .logging_setup import setup_logging
from .news.models import RELATIONS, NewsBoard
from .provider import LiveProvider, Provider
from .quant.engine import QuantBoard
from .quant.options import options_explain, options_summary
from .quant.scoring import explain_text, score_summary
from .relationships import catalyst_label, peers_snippet
from .watchlist import Watchlist


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock_terminal",
        description="Read-only stock news, quant read and validity terminal. Places no orders.",
    )
    parser.add_argument("--check", action="store_true", help="fetch each source once, print its status, and exit")
    parser.add_argument("--news", metavar="TICKER", help="print the tagged news feed for one ticker and exit")
    parser.add_argument("--peers", metavar="TICKER", help="print Finnhub's peers for a ticker as a relationships.yaml snippet")
    parser.add_argument("--reactions", action="store_true", help="print average price moves after headlines, by tag and catalyst")
    parser.add_argument("--score", metavar="TICKER", help="print one ticker's long-term score with the explain view")
    parser.add_argument("--options", metavar="TICKER", help="print one ticker's call / put check with the explain view")
    parser.add_argument("--universe", action="store_true", help="refresh the universe's closes, fundamentals and news now")
    parser.add_argument("--signals", action="store_true", help="print the signal log")
    parser.add_argument("--rededupe", action="store_true", help="recompute every duplicate mark in the news store with the current rule")
    parser.add_argument("--config", default="config.yaml", help="settings file (default: config.yaml)")
    parser.add_argument("--watchlist", default="watchlist.yaml", help="watchlist file (default: watchlist.yaml)")
    parser.add_argument("--env", default=".env", help="secrets file (default: .env)")
    return parser


def _plain(markup: str) -> str:
    """Rich markup -> plain text for the console."""
    from rich.text import Text

    return Text.from_markup(markup).plain


def _progress(text: str) -> None:
    print(f"  ... {text}", flush=True)


def print_news_status(news_board: NewsBoard, tickers: list[str]) -> None:
    print("\nNews sources:")
    for name, state in news_board.source_status.items():
        print(f"  {name:<22} {state}")
    print(f"\nSentiment model: {news_board.scorer_status}")
    print(f"This refresh: {news_board.new_items} new headlines, {news_board.duplicates} duplicates of known stories")
    print("\nHeadlines per ticker (last 7 days of news, 30 of filings):")
    for ticker in tickers:
        feed = news_board.feeds.get(ticker, [])
        counts = {rel: sum(1 for e in feed if e.relation == rel) for rel in RELATIONS}
        detail = "  ".join(f"{rel} {n}" for rel, n in counts.items())
        print(f"  {ticker:<6} {len(feed):>3} total   {detail}")
        if ticker in news_board.map_notes:
            print(f"         note: {news_board.map_notes[ticker]}")


def print_quant_status(board: QuantBoard, tickers: list[str]) -> None:
    print(f"\nLong-term score: {board.status}")
    for note in board.notes:
        print(f"  note: {note}")
    for ticker in tickers:
        score = board.scores.get(ticker)
        if score is None or score.composite is None:
            print(f"  {ticker:<6} no score" + (f" ({score.notes[0]})" if score and score.notes else ""))
            continue
        label = score.label or "no label"
        pct = f"{score.percentile:.0f}th pct" if score.percentile is not None else ""
        print(f"  {ticker:<6} {label:<12} composite {score.composite:+.2f}  {pct}  as of {score.as_of}")
    if board.options:
        print("\nOptions check:")
        for ticker in tickers:
            result = board.options.get(ticker)
            earnings = board.earnings.get(ticker)
            when = earnings.value if earnings is not None and earnings.ok else f"-- ({earnings.note})" if earnings is not None else "--"
            if result is None:
                print(f"  {ticker:<6} no check")
                continue
            contract = result.contract.symbol if result.contract else "no contract"
            d = result.direction.value
            print(f"  {ticker:<6} {result.label:<15} direction {d:+.2f}  {contract}  next earnings {when}" if d is not None else f"  {ticker:<6} {result.label:<15} direction --  {contract}  next earnings {when}")
            for f in result.failed:
                print(f"         failed {f}")
    for event in board.signals:
        print(f"  signal logged: {event}")


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

    print("\nPulling news (the first run loads the sentiment model, which can take a minute)...")
    news_board = provider.refresh_news(list(watchlist.tickers))
    print_news_status(news_board, list(watchlist.tickers))

    print("\nScoring (the first run pulls a year of closes and the fundamentals for the whole universe)...")
    quant_board = provider.refresh_quant(list(watchlist.tickers), progress=_progress)
    print_quant_status(quant_board, list(watchlist.tickers))

    print(f"\n{ATTRIBUTION}")
    return 0


def run_news(provider: Provider, ticker: str) -> int:
    ticker = ticker.upper()
    news_board = provider.refresh_news([ticker])
    feed = news_board.feeds.get(ticker, [])
    print(f"{ticker}: {len(feed)} headlines, newest first. Sentiment model: {news_board.scorer_status}")
    if ticker in news_board.map_notes:
        print(f"note: {news_board.map_notes[ticker]}")
    for entry in feed:
        print()
        print(_plain(entry_markup(entry)))
    print("\nSources:")
    for name, state in news_board.source_status.items():
        print(f"  {name:<22} {state}")
    return 0


def run_score(provider: Provider, ticker: str) -> int:
    ticker = ticker.upper()
    board = provider.refresh_quant([ticker], progress=_progress)
    print(f"\n{board.status}")
    for note in board.notes:
        print(f"note: {note}")
    score = board.scores.get(ticker)
    if score is None:
        print(f"{ticker}: no score")
        return 1
    print()
    print(_plain(score_summary(score)))
    print()
    print(_plain(explain_text(score, board.universe_name)))
    for event in board.signals:
        print(f"\nsignal logged: {event}")
    return 0


def run_options(provider: Provider, ticker: str) -> int:
    ticker = ticker.upper()
    board = provider.refresh_quant([ticker], progress=_progress)
    print(f"\n{board.status}")
    result = board.options.get(ticker)
    if result is None:
        print(f"{ticker}: no options check (options.enabled false, or no data)")
        return 1
    print()
    print(_plain(options_summary(result)))
    print()
    print(_plain(options_explain(result)))
    for event in board.signals:
        print(f"\nsignal logged: {event}")
    return 0


def run_universe(provider: Provider, watchlist: Watchlist) -> int:
    board = provider.refresh_quant(list(watchlist.tickers), progress=_progress, force=True)
    print_quant_status(board, list(watchlist.tickers))
    return 0


def run_signals(provider: Any) -> int:
    signals = provider.market.signals()
    if not signals:
        print("No signals logged yet.")
        return 0
    print(f"{'id':>4}  {'ticker':<7}{'kind':<10}{'label':<12}{'dir':>4}  {'opened':<17}{'open':>9}  {'closed':<17}{'close':>9}  note")
    for s in signals:
        opened = s.opened_at.strftime("%Y-%m-%d %H:%M") if s.opened_at else "?"
        closed = s.closed_at.strftime("%Y-%m-%d %H:%M") if s.closed_at else "open"
        open_price = f"{s.open_price:.2f}" if s.open_price is not None else "--"
        close_price = f"{s.close_price:.2f}" if s.close_price is not None else "--"
        print(f"{s.id:>4}  {s.ticker:<7}{s.kind:<10}{s.label:<12}{s.direction:>4}  {opened:<17}{open_price:>9}  {closed:<17}{close_price:>9}  {s.note}")
    return 0


def run_peers(provider: Any, ticker: str) -> int:
    ticker = ticker.upper()
    dp = provider.peers(ticker)
    if not dp.ok:
        print(f"Finnhub peers for {ticker}: [{dp.status.upper()}] {dp.note}")
        return 1
    peers = list(dp.value)
    print(f"Finnhub lists these peers for {ticker}: {', '.join(peers) or '(none)'}")
    print("\nPaste this under `tickers:` in relationships.yaml, then set sector_etf and topics by hand:\n")
    print(peers_snippet(ticker, peers))
    return 0


def run_reactions(provider: Any) -> int:
    rows = provider.store.reaction_summary()
    counts = provider.store.reaction_counts()
    print("Reactions tracked: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else "No reactions tracked yet.")
    if not rows:
        return 0
    print("\nAverage move after a headline, by ticker, relation and catalyst (n = headlines with that slot filled):")
    print(f"  {'ticker':<7}{'relation':<8}{'catalyst':<17}{'n':>4}  {'+1h':>14}  {'+1d':>14}  {'+5d':>14}")
    for row in rows:
        def cell(avg: Any, n: Any) -> str:
            return f"{avg:+.2f}% (n={int(n)})" if avg is not None else f"-- (n={int(n or 0)})"
        print(
            f"  {row['symbol']:<7}{row['relation']:<8}{catalyst_label(row['catalyst']):<17}{int(row['n']):>4}  "
            f"{cell(row['avg_1h'], row['n_1h']):>14}  {cell(row['avg_1d'], row['n_1d']):>14}  {cell(row['avg_5d'], row['n_5d']):>14}"
        )
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
    if args.news:
        return run_news(provider, args.news)
    if args.peers:
        return run_peers(provider, args.peers)
    if args.reactions:
        return run_reactions(provider)
    if args.score:
        return run_score(provider, args.score)
    if args.options:
        return run_options(provider, args.options)
    if args.universe:
        return run_universe(provider, watchlist)
    if args.signals:
        return run_signals(provider)
    if args.rededupe:
        originals, duplicates = provider.store.rededupe()
        print(f"news store re-deduplicated: {originals} originals, {duplicates} duplicates")
        return 0
    StockTerminalApp(cfg, watchlist, provider, logger=logger).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

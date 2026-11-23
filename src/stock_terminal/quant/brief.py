"""The brief: today's watchlist summary as a markdown file, from what the screen already holds."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from rich.text import Text

NY = ZoneInfo("America/New_York")   # the brief is dated and named by New York time, like the screen's Refreshed stamp

from ..news.models import DIRECT


def _plain(markup: str) -> str:
    return Text.from_markup(markup).plain


def build_brief(cfg: dict[str, Any], board: Any, news_board: Any, quant_board: Any, now: datetime, dates_line: str = "") -> str:
    from .options import options_summary
    from .scoring import score_summary
    from .store import LONG_TERM, OPTIONS
    from .validity import validity_summary
    from .backtest import backtest_summary

    local = now.astimezone(NY)
    lines = [f"# Watchtower brief, {local.strftime('%A %B %d, %Y, %I:%M %p ET').replace(' 0', ' ')}", ""]
    lines.append("Read-only. Labels are Favorable / Neutral / Unfavorable and Call candidate / Put candidate / No trade, never buy or sell. "
                 "Every label carries its validity status; UNPROVEN means fewer than 30 scored signals.")
    lines.append("")
    if board is not None:
        strip = []
        for symbol in cfg["top_strip"]["equities"]:
            q = board.quotes.get(symbol)
            if q is not None:
                strip.append(f"{symbol} {q.price.display()} ({q.change_pct.display('{:+.2f}%')})")
        for label, dp in board.fred.items():
            fmt = "{:.2f}%" if "DGS" in dp.source else "{:.2f}"
            strip.append(f"{label} {dp.display(fmt)}" + (f" ({dp.as_of.strftime('%b %d')})" if dp.as_of else ""))
        lines.append("## Market")
        lines.append("")
        lines.append("  ".join(strip) if strip else "no quotes")
        lines.append("")
    if dates_line:
        lines.append(dates_line)
        lines.append("")
    tickers = list(quant_board.scores.keys()) if quant_board is not None else []
    if board is not None and not tickers:
        tickers = [s for s in board.quotes if s not in cfg["top_strip"]["equities"]]
    for ticker in tickers:
        lines.append(f"## {ticker}")
        lines.append("")
        q = board.quotes.get(ticker) if board is not None else None
        if q is not None:
            lines.append(f"Price {q.price.display()} ({q.change_pct.display('{:+.2f}%')}), data {q.price.status}.")
        if quant_board is not None:
            score = quant_board.scores.get(ticker)
            if score is not None:
                lines.append("")
                lines.append("```")
                lines.append(_plain(score_summary(score, quant_board.validity_text(LONG_TERM))))
                lines.append("```")
            result = quant_board.options.get(ticker)
            if result is not None:
                lines.append("")
                lines.append("```")
                lines.append(_plain(options_summary(result, quant_board.validity_text(OPTIONS))))
                lines.append("```")
        if news_board is not None:
            feed = [e for e in news_board.feeds.get(ticker, []) if e.relation == DIRECT][:5]
            if feed:
                lines.append("")
                lines.append("Newest DIRECT headlines:")
                for e in feed:
                    sent = f"{e.item.sentiment:+.2f}" if e.item.sentiment is not None else "--"
                    lines.append(f"- [{sent}] {e.item.headline} ({e.item.source.split('/')[-1]}, {e.item.published_at.strftime('%b %d %H:%M')} UTC)")
        lines.append("")
    if quant_board is not None:
        lines.append("## Validity")
        lines.append("")
        lines.append("```")
        lines.append(_plain(validity_summary(quant_board.validity.get(LONG_TERM))))
        lines.append("")
        lines.append(_plain(validity_summary(quant_board.validity.get(OPTIONS))))
        lines.append("")
        lines.append(_plain(backtest_summary(getattr(quant_board, "backtest", None))))
        lines.append("```")
        lines.append("")
    lines.append("This product uses the FRED API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.")
    return "\n".join(lines) + "\n"


def write_brief(directory: str | Path, text: str, now: datetime) -> Path:
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{now.astimezone(NY).strftime('%Y-%m-%d')}.md"
    path.write_text(text, encoding="utf-8")
    return path

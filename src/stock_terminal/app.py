"""The screen.

Top strip (SPY, QQQ, VIX, 10-year, market status, data-delay notice), the watchlist
table on the left, the news feed for the highlighted ticker in the middle, the detail
tabs on the right (Score live from Phase 3; Options and Validity later), and a
collapsible log panel. Keys: a add, d delete, r refresh, f cycle the news filter, o open
the highlighted headline, ? explain the score, l log, q quit. All data arrives through a
Provider so the screen never talks to a network client directly. Prices, news and the
score refresh on their own workers and timers.
"""
from __future__ import annotations

import logging
import webbrowser
from collections import deque
from datetime import datetime
from typing import Any, Deque, Optional
from zoneinfo import ZoneInfo

from rich.markup import escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Collapsible,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from .datapoint import DELAYED_MARK, FRESH, STALE_MARK, human_age, utcnow
from .logging_setup import CallbackHandler
from .news.models import RELATIONS, FeedEntry, NewsBoard
from .news.reactions import describe as describe_reaction
from .provider import Board, Provider
from .quant.engine import QuantBoard
from .quant.options import options_explain, options_summary
from .quant.scoring import explain_text, score_summary
from .quant.store import LONG_TERM, OPTIONS
from .quant.validity import validity_explain, validity_summary
from rich.text import Text
from .relationships import catalyst_label
from .watchlist import Watchlist

NY = ZoneInfo("America/New_York")
COLUMNS = ("Ticker", "Price", "Chg %", "LT", "Opt", "Earn", "Validity", "Data")
# No signal has ever been scored, so by the spec's own definition every label is UNPROVEN (under 30 scored).
VALIDITY_PLACEHOLDER = "UNPROVEN 0/30"
PHASE_PLACEHOLDER = "--"
LEGEND = f"{STALE_MARK} stale  {DELAYED_MARK} delayed  -- missing"
NEWS_FILTERS = ("ALL",) + RELATIONS
RELATION_STYLE = {"DIRECT": "bold green", "PEER": "cyan", "SECTOR": "magenta", "MACRO": "yellow", "FILING": "bold blue"}


def fmt_time_ny(dt: Optional[datetime]) -> str:
    """Thu Nov 12 4:00 PM ET. Windows strftime has no %-I, so the leading zero is stripped by hand."""
    if dt is None:
        return "?"
    local = dt.astimezone(NY)
    hour = local.strftime("%I").lstrip("0") or "12"
    return local.strftime(f"%a %b %d {hour}:%M %p ET")


def _parse_iso(text: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(text)) if text else None
    except ValueError:
        return None


def short_source(source: str) -> str:
    """alpaca/benzinga -> benzinga, edgar/8-K -> 8-K, finnhub/Reuters -> Reuters."""
    return source.split("/", 1)[1] if "/" in source else source


def entry_markup(entry: FeedEntry, now: Optional[datetime] = None) -> str:
    """Three lines of Rich markup for one headline: tags, the headline, source and reaction."""
    now = now or utcnow()
    item = entry.item
    age = human_age((now - item.published_at).total_seconds())
    style = RELATION_STYLE.get(entry.relation, "")
    sentiment = f"{item.sentiment:+.2f}" if item.sentiment is not None else "--"
    line1 = f"[dim]{age:>3}[/] [{style}]{entry.relation:<6}[/] {escape(catalyst_label(item.catalyst)):<16} sent {sentiment}"
    line2 = escape(item.headline)
    line3 = f"[dim]{escape(short_source(item.source))}  {escape(describe_reaction(entry.reaction))}[/]"
    return f"{line1}\n{line2}\n{line3}"


class TickerPrompt(ModalScreen[Optional[str]]):
    DEFAULT_CSS = """
    TickerPrompt { align: center middle; }
    #prompt { width: 56; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt"):
            yield Label("Add a ticker. Enter to confirm, Esc to cancel.")
            yield Input(placeholder="e.g. TSLA", id="ticker-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ExplainScreen(ModalScreen[None]):
    """The ? view: the formula, every input and its source for the highlighted ticker's score."""

    DEFAULT_CSS = """
    ExplainScreen { align: center middle; }
    #explain { width: 90%; height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
    """
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("question_mark", "close", "Close", key_display="?"),
        Binding("q", "close", "Close"),
    ]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="explain"):
            yield Static(self.text, id="explain-text")

    def on_mount(self) -> None:
        self.query_one("#explain", VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss(None)


class StockTerminalApp(App[None]):
    TITLE = "Stock Terminal: read-only, no orders, ever"
    CSS = """
    #top-strip { height: auto; padding: 0 1; background: $panel; border-bottom: solid $primary; }
    #banner { height: auto; padding: 0 1; background: $error; color: $text; display: none; }
    #main { height: 1fr; }
    #watchlist { width: 46%; min-width: 66; }
    #news { width: 1fr; min-width: 20; border-left: solid $primary; }
    #news-head { height: auto; padding: 0 1; background: $panel; }
    #news-list { height: 1fr; }
    #news-list > ListItem { height: auto; padding: 0 1; }
    #news-list Static { width: 1fr; }
    #detail { width: 1fr; min-width: 24; border-left: solid $primary; }
    #score-pane { padding: 0 1; }
    #log-panel { height: auto; }
    #log { height: 8; }
    """
    BINDINGS = [
        Binding("a", "add_ticker", "Add"),
        Binding("d", "delete_ticker", "Delete"),
        Binding("r", "refresh", "Refresh"),
        Binding("f", "cycle_filter", "Filter"),
        Binding("o", "open_link", "Open"),
        Binding("question_mark", "explain", "Explain", key_display="?"),
        Binding("l", "toggle_log", "Log"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        cfg: dict[str, Any],
        watchlist: Watchlist,
        provider: Provider,
        periodic_refresh: bool = True,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.watchlist = watchlist
        self.provider = provider
        # Not stored as auto_refresh: Textual already owns an attribute of that name (its repaint interval).
        self.periodic_refresh = periodic_refresh
        self.logger = logger or logging.getLogger("stock_terminal")
        self.board: Optional[Board] = None
        self.news_board: Optional[NewsBoard] = None
        self.quant_board: Optional[QuantBoard] = None
        self.quant_status = "score: loading..."
        self.news_filter = "ALL"
        self.news_head_text = "News: loading..."
        self.score_text = ""
        self.options_text = ""
        self.validity_text = ""
        self.shown_entries: list[FeedEntry] = []
        self.strip_text = "Loading..."
        self._log_lines: Deque[str] = deque(maxlen=500)
        self._log_handler: Optional[CallbackHandler] = None

    # ----- layout -----
    def compose(self) -> ComposeResult:
        yield Header()  # no clock: the strip's Refreshed stamp, in ET, is the time of record
        yield Static(self.strip_text, id="top-strip")
        yield Static("", id="banner")
        with Horizontal(id="main"):
            yield DataTable(id="watchlist")
            with Vertical(id="news"):
                yield Static(self.news_head_text, id="news-head")
                yield ListView(id="news-list")
            with TabbedContent(id="detail"):
                with TabPane("Score", id="tab-score"):
                    with VerticalScroll():
                        yield Static("Score: loading...", id="score-pane")
                with TabPane("Options", id="tab-options"):
                    with VerticalScroll():
                        yield Static("Options: loading...", id="options-pane")
                with TabPane("Validity", id="tab-validity"):
                    with VerticalScroll():
                        yield Static("Validity: loading...", id="validity-pane")
        with Collapsible(title="Log (l to toggle)", collapsed=True, id="log-panel"):
            yield RichLog(id="log", max_lines=500, wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#watchlist", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns(*COLUMNS)
        self._render_table()
        self._attach_log_panel()
        self.logger.info("app started; watchlist: %s", ", ".join(self.watchlist.tickers) or "(empty)")
        self.refresh_data()
        self.refresh_news()
        self.refresh_quant()
        if self.periodic_refresh:
            self.set_interval(float(self.cfg["refresh_seconds"]), self.refresh_data)
            self.set_interval(float(self.cfg["news"]["refresh_seconds"]), self.refresh_news)
            self.set_interval(float(self.cfg["quant"]["refresh_seconds"]), self.refresh_quant)

    def on_unmount(self) -> None:
        if self._log_handler is not None:
            logging.getLogger("stock_terminal").removeHandler(self._log_handler)

    # ----- log panel -----
    def _attach_log_panel(self) -> None:
        # Log records can arrive from worker threads, so they queue up and the app thread drains them.
        self._log_handler = CallbackHandler(self._log_lines.append)
        logging.getLogger("stock_terminal").addHandler(self._log_handler)
        self.set_interval(1.0, self._drain_log)

    def _drain_log(self) -> None:
        panel = self.query_one("#log", RichLog)
        while self._log_lines:
            panel.write(self._log_lines.popleft())

    # ----- data -----
    def action_refresh(self) -> None:
        self.refresh_data()
        self.refresh_news()
        self.refresh_quant()

    @work(thread=True, exclusive=True, group="refresh", exit_on_error=False)
    def refresh_data(self) -> None:
        symbols = list(dict.fromkeys(list(self.cfg["top_strip"]["equities"]) + list(self.watchlist.tickers)))
        try:
            board = self.provider.refresh(symbols)
        except Exception as exc:  # noqa: BLE001  a provider bug must show up in the log panel, not kill the screen
            self.logger.exception("refresh failed")
            self.call_from_thread(self.notify, f"Refresh failed: {exc}", severity="error")
            return
        self.call_from_thread(self.apply_board, board)

    @work(thread=True, exclusive=True, group="news", exit_on_error=False)
    def refresh_news(self) -> None:
        tickers = list(self.watchlist.tickers)
        try:
            news_board = self.provider.refresh_news(tickers)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("news refresh failed")
            self.call_from_thread(self.notify, f"News refresh failed: {exc}", severity="error")
            return
        self.call_from_thread(self.apply_news_board, news_board)

    @work(thread=True, exclusive=True, group="quant", exit_on_error=False)
    def refresh_quant(self) -> None:
        tickers = list(self.watchlist.tickers)
        try:
            self.provider.refresh_quant(
                tickers,
                on_board=lambda b: self.call_from_thread(self.apply_quant_board, b),
                progress=lambda text: self.call_from_thread(self.set_quant_status, f"score: {text}"),
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("score refresh failed")
            self.call_from_thread(self.set_quant_status, f"score failed: {exc}")
            self.call_from_thread(self.notify, f"Score refresh failed: {exc}", severity="error")

    def apply_board(self, board: Board) -> None:
        self.board = board
        self._render_top_strip()
        self._render_table()

    def apply_news_board(self, news_board: NewsBoard) -> None:
        self.news_board = news_board
        self._render_news()

    def apply_quant_board(self, quant_board: QuantBoard) -> None:
        self.quant_board = quant_board
        self.quant_status = f"score: {quant_board.status}"
        for event in quant_board.signals:
            self.notify(f"Label change: {event}", title="Signal logged")
        for event in quant_board.scored:
            self.notify(event, title="Signal scored")
        self._render_banner()
        self._render_table()
        self._render_score()
        self._render_validity()

    def set_quant_status(self, text: str) -> None:
        self.quant_status = text
        self._render_score()

    # ----- rendering -----
    def _render_top_strip(self) -> None:
        board = self.board
        parts: list[str] = []
        for symbol in self.cfg["top_strip"]["equities"]:
            quote = board.quotes.get(symbol) if board else None
            if quote is None:
                parts.append(f"{symbol} --")
            else:
                parts.append(f"{symbol} {quote.price.display()} ({quote.change_pct.display('{:+.2f}%')})")
        for label, dp in (board.fred.items() if board else []):
            fmt = "{:.2f}%" if "DGS" in dp.source else "{:.2f}"
            when = dp.as_of.strftime("%b %d") if dp.as_of else "?"
            parts.append(f"{label} {dp.display(fmt)} ({when})")
        line1 = "   ".join(parts)

        if board is None:
            status = "Loading..."
        elif board.clock.ok:
            if board.market_open:
                status = f"Market OPEN, closes {fmt_time_ny(_parse_iso(board.clock.value.get('next_close')))}"
            else:
                status = f"Market CLOSED, opens {fmt_time_ny(_parse_iso(board.clock.value.get('next_open')))}"
        else:
            status = f"Market status unknown ({board.clock.note})"

        age = board.newest_trade_age() if board else None
        if age is None:
            delay = "No trade data"
        elif board is not None and board.market_open and age > float(self.cfg["alpaca"]["delayed_after_seconds"]):
            delay = f"DELAYED: newest IEX trade {human_age(age)} ago"
        else:
            delay = f"Newest IEX trade {human_age(age)} ago"

        notes = sorted({q.price.note for q in (board.quotes.values() if board else []) if not q.price.ok and q.price.note})
        missing = f"Missing: {'; '.join(notes)}" if notes else ""
        refreshed = f"Refreshed {fmt_time_ny(board.refreshed_at)}" if board else ""
        line2 = " | ".join(p for p in (status, delay, missing, refreshed, LEGEND) if p)
        self.strip_text = f"{line1}\n{line2}"
        self.query_one("#top-strip", Static).update(self.strip_text)

    def _selected_ticker(self) -> Optional[str]:
        table = self.query_one("#watchlist", DataTable)
        if table.row_count == 0:
            return None
        try:
            return str(table.get_row_at(table.cursor_coordinate.row)[0])
        except Exception:  # noqa: BLE001  cursor off the table during a redraw
            return None

    def _cell(self, text: str) -> Any:
        """In virtual mode the labels are greyed out: the book's rule, keep recording, stop trusting."""
        if self.quant_board is not None and self.quant_board.virtual_mode:
            return Text(text, style="dim strike")
        return text

    def _lt_label(self, ticker: str) -> str:
        if self.quant_board is None:
            return "..."
        score = self.quant_board.scores.get(ticker)
        return score.short_label if score is not None else "--"

    def _validity_label(self) -> str:
        if self.quant_board is None:
            return VALIDITY_PLACEHOLDER
        return self.quant_board.validity_text(LONG_TERM)

    def _render_banner(self) -> None:
        banner = self.query_one("#banner", Static)
        board = self.quant_board
        if board is None or not board.virtual_mode:
            banner.display = False
            return
        kinds = [k.replace("_", "-") for k, r in board.validity.items() if r.virtual_mode]
        banner.update(f"VIRTUAL MODE: the {' and '.join(kinds)} labels test as RANDOM-LIKE. The app keeps recording them; stop trusting them until the tests say otherwise. Details in the Validity tab.")
        banner.display = True

    def _opt_label(self, ticker: str) -> str:
        if self.quant_board is None:
            return "..."
        result = self.quant_board.options.get(ticker)
        return result.short_label if result is not None else "--"

    def _earn_label(self, ticker: str) -> str:
        """Days to the next earnings date, from Finnhub's calendar."""
        if self.quant_board is None:
            return "..."
        dp = self.quant_board.earnings.get(ticker)
        if dp is None or not dp.ok or dp.as_of is None:
            return "--"
        days = (dp.as_of.date() - utcnow().date()).days
        return f"{days}d" if days >= 0 else "--"

    def _render_table(self) -> None:
        table = self.query_one("#watchlist", DataTable)
        selected = self._selected_ticker()
        table.clear()
        for ticker in self.watchlist.tickers:
            quote = self.board.quotes.get(ticker) if self.board else None
            if quote is None:
                price, change = "--", "--"
                data = "loading" if self.board is None else "MISSING"
            else:
                price = quote.price.display()
                change = quote.change_pct.display("{:+.2f}%")
                data = "ok" if quote.price.status == FRESH else quote.price.status.upper()
            table.add_row(
                ticker, price, change,
                self._cell(self._lt_label(ticker)), self._cell(self._opt_label(ticker)), self._earn_label(ticker),
                self._validity_label(), data,
                key=ticker,
            )
        if selected in self.watchlist.tickers:
            table.move_cursor(row=table.get_row_index(selected))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._render_news()
        self._render_score()

    def _news_head(self, ticker: Optional[str], shown: int, total: int) -> str:
        board = self.news_board
        if ticker is None:
            return "News: add a ticker with a"
        if board is None:
            return f"{ticker} news: loading..."
        when = fmt_time_ny(board.refreshed_at) if board.refreshed_at else "?"
        line1 = f"{ticker} news  filter {self.news_filter}  {shown} of {total}  refreshed {when}"
        # One line per source family (alpaca, finnhub, edgar): "finnhub news AAPL" and "finnhub general" failing for
        # the same reason would otherwise fill the narrow pane. Other tickers' entries are left out.
        problems: dict[str, str] = {}
        for name, state in board.source_status.items():
            if state.startswith("ok"):
                continue
            words = name.split()
            if len(words) > 2 and words[-1] != ticker and words[-1] in self.watchlist.tickers:
                continue
            problems.setdefault(words[0], state)
        lines = [line1, f"sentiment {board.scorer_status}"]
        if ticker in board.map_notes:
            lines.append(board.map_notes[ticker])
        if problems:
            lines.append("sources: " + "; ".join(f"{family} {state}" for family, state in problems.items()))
        return "\n".join(lines)

    def _render_news(self) -> None:
        ticker = self._selected_ticker()
        listing = self.query_one("#news-list", ListView)
        entries = self.news_board.feeds.get(ticker, []) if (self.news_board and ticker) else []
        shown = [e for e in entries if self.news_filter == "ALL" or e.relation == self.news_filter]
        self.shown_entries = shown
        self.news_head_text = self._news_head(ticker, len(shown), len(entries))
        self.query_one("#news-head", Static).update(self.news_head_text)
        listing.clear()
        now = utcnow()
        if ticker and self.news_board and not shown:
            listing.append(ListItem(Static("No headlines in the window for this filter.")))
        for entry in shown:
            listing.append(ListItem(Static(entry_markup(entry, now))))

    def _render_score(self) -> None:
        ticker = self._selected_ticker()
        lines = [f"[dim]{escape(self.quant_status)}[/]"]
        if ticker is None:
            lines.append("Add a ticker with a.")
        elif self.quant_board is None:
            lines.append(f"{ticker}: score not computed yet.")
        else:
            score = self.quant_board.scores.get(ticker)
            lines.append(score_summary(score, self.quant_board.validity_text(LONG_TERM)) if score is not None else f"{ticker}: No score yet.")
            for note in self.quant_board.notes:
                lines.append(f"[yellow]{escape(note)}[/]")
        self.score_text = "\n".join(lines)
        self.query_one("#score-pane", Static).update(self.score_text)
        self._render_options(ticker)

    def _render_options(self, ticker: Optional[str]) -> None:
        lines = [f"[dim]{escape(self.quant_status)}[/]"]
        if ticker is None:
            lines.append("Add a ticker with a.")
        elif self.quant_board is None:
            lines.append(f"{ticker}: options check not computed yet.")
        else:
            result = self.quant_board.options.get(ticker)
            lines.append(options_summary(result, self.quant_board.validity_text(OPTIONS)) if result is not None else f"{ticker}: No options check yet.")
        self.options_text = "\n".join(lines)
        self.query_one("#options-pane", Static).update(self.options_text)

    def _render_validity(self) -> None:
        board = self.quant_board
        if board is None:
            self.validity_text = "Validity: loading..."
        else:
            parts = [validity_summary(board.validity.get(LONG_TERM)), "", validity_summary(board.validity.get(OPTIONS)), "", validity_explain()]
            self.validity_text = "\n".join(parts)
        self.query_one("#validity-pane", Static).update(self.validity_text)

    # ----- actions -----
    def action_add_ticker(self) -> None:
        self.push_screen(TickerPrompt(), self._ticker_added)

    def _ticker_added(self, value: Optional[str]) -> None:
        if value is None:
            return
        added = self.watchlist.add(value)
        if added is None:
            shown = value.strip().upper() or "(empty)"
            self.notify(f"Not added: {shown} is not a valid ticker or is already listed", severity="warning")
            return
        self.logger.info("added %s", added)
        self._render_table()
        self.notify(f"Added {added}")
        self.refresh_data()
        self.refresh_news()
        self.refresh_quant()

    def action_delete_ticker(self) -> None:
        ticker = self._selected_ticker()
        if ticker is None:
            self.notify("Nothing to delete", severity="warning")
            return
        self.watchlist.remove(ticker)
        self.logger.info("removed %s", ticker)
        self._render_table()
        self._render_news()
        self._render_score()
        self.notify(f"Removed {ticker}")

    def action_cycle_filter(self) -> None:
        index = NEWS_FILTERS.index(self.news_filter) if self.news_filter in NEWS_FILTERS else 0
        self.news_filter = NEWS_FILTERS[(index + 1) % len(NEWS_FILTERS)]
        self._render_news()

    def action_open_link(self) -> None:
        listing = self.query_one("#news-list", ListView)
        index = listing.index
        if index is None or not (0 <= index < len(self.shown_entries)):
            self.notify("Move into the news list (Tab, then arrows) and press o on a headline", severity="warning")
            return
        url = self.shown_entries[index].item.url
        if not url:
            self.notify("This item has no link", severity="warning")
            return
        self.logger.info("opening %s", url)
        webbrowser.open(url)

    def action_explain(self) -> None:
        ticker = self._selected_ticker()
        score = self.quant_board.scores.get(ticker) if (self.quant_board and ticker) else None
        if score is None:
            self.notify(f"No score to explain yet ({self.quant_status})", severity="warning")
            return
        text = explain_text(score, self.quant_board.universe_name, self.quant_board.validity_text(LONG_TERM))
        result = self.quant_board.options.get(ticker)
        if result is not None:
            text += "\n\n" + options_explain(result, self.quant_board.validity_text(OPTIONS))
        self.push_screen(ExplainScreen(text))

    def action_toggle_log(self) -> None:
        panel = self.query_one("#log-panel", Collapsible)
        panel.collapsed = not panel.collapsed

"""The screen.

Top strip (SPY, QQQ, VIX, 10-year, market status, data-delay notice), the watchlist
table on the left, the news feed in the middle (Phase 2), the detail tabs on the
right (Phases 3 to 5), and a collapsible log panel. Keys: a add, d delete, r refresh,
? explain, l log, q quit. All data arrives through a Provider so the screen never
talks to a network client directly.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Any, Deque, Optional
from zoneinfo import ZoneInfo

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Collapsible,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from .datapoint import DELAYED_MARK, STALE_MARK, human_age
from .logging_setup import CallbackHandler
from .provider import Board, Provider
from .watchlist import Watchlist

NY = ZoneInfo("America/New_York")
COLUMNS = ("Ticker", "Price", "Chg %", "Long-term", "Options", "Earnings", "Validity", "Data")
# No signal has ever been produced, so by the spec's own definition every label is UNPROVEN (under 30 scored).
VALIDITY_PLACEHOLDER = "UNPROVEN 0/30"
PHASE_PLACEHOLDER = "--"
LEGEND = f"{STALE_MARK} stale  {DELAYED_MARK} delayed  -- missing"


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


class StockTerminalApp(App[None]):
    TITLE = "Stock Terminal"
    SUB_TITLE = "read-only, no orders, ever"
    CSS = """
    #top-strip { height: 3; padding: 0 1; background: $panel; border-bottom: solid $primary; }
    #main { height: 1fr; }
    #watchlist { width: 46%; }
    #news { width: 27%; border-left: solid $primary; padding: 0 1; }
    #detail { width: 27%; border-left: solid $primary; }
    #log-panel { height: auto; }
    #log { height: 8; }
    """
    BINDINGS = [
        Binding("a", "add_ticker", "Add"),
        Binding("d", "delete_ticker", "Delete"),
        Binding("r", "refresh", "Refresh"),
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
        self.strip_text = "Loading..."
        self._log_lines: Deque[str] = deque(maxlen=500)
        self._log_handler: Optional[CallbackHandler] = None

    # ----- layout -----
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(self.strip_text, id="top-strip")
        with Horizontal(id="main"):
            yield DataTable(id="watchlist")
            yield Static(
                "News feed arrives in Phase 2.\n\nSelect a ticker on the left; its DIRECT, PEER, SECTOR, "
                "MACRO and FILING headlines will list here, newest first, each with a sentiment score.",
                id="news",
            )
            with TabbedContent(id="detail"):
                with TabPane("Score", id="tab-score"):
                    yield Static(
                        "Long-term score arrives in Phase 3. Press ? there for the formula, every input and its source.",
                        id="score-pane",
                    )
                with TabPane("Options", id="tab-options"):
                    yield Static("Call / put check arrives in Phase 4.", id="options-pane")
                with TabPane("Validity", id="tab-validity"):
                    yield Static(
                        "Validity engine arrives in Phase 5. Until 30 signals are scored, every label reads UNPROVEN.",
                        id="validity-pane",
                    )
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
        if self.periodic_refresh:
            self.set_interval(float(self.cfg["refresh_seconds"]), self.refresh_data)

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

    def apply_board(self, board: Board) -> None:
        self.board = board
        self._render_top_strip()
        self._render_table()

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

    def _render_table(self) -> None:
        table = self.query_one("#watchlist", DataTable)
        selected = self._selected_ticker()
        table.clear()
        for ticker in self.watchlist.tickers:
            quote = self.board.quotes.get(ticker) if self.board else None
            if quote is None:
                price, change = "--", "--"
                data = "loading" if self.board is None else "MISSING: not in response"
            else:
                price = quote.price.display()
                change = quote.change_pct.display("{:+.2f}%")
                data = quote.price.status_label() or quote.price.source
            table.add_row(
                ticker, price, change,
                PHASE_PLACEHOLDER, PHASE_PLACEHOLDER, PHASE_PLACEHOLDER,
                VALIDITY_PLACEHOLDER, data,
                key=ticker,
            )
        if selected in self.watchlist.tickers:
            table.move_cursor(row=table.get_row_index(selected))

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

    def action_delete_ticker(self) -> None:
        ticker = self._selected_ticker()
        if ticker is None:
            self.notify("Nothing to delete", severity="warning")
            return
        self.watchlist.remove(ticker)
        self.logger.info("removed %s", ticker)
        self._render_table()
        self.notify(f"Removed {ticker}")

    def action_explain(self) -> None:
        self.notify(
            "The explain view arrives in Phase 3 with the long-term score: the formula, every input and its source.",
            title="? not wired yet",
        )

    def action_toggle_log(self) -> None:
        panel = self.query_one("#log-panel", Collapsible)
        panel.collapsed = not panel.collapsed

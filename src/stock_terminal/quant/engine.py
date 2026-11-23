"""The long-term score, end to end: universe data, factors, z-scores, labels, signal log.

One `refresh`:
1. Read universe.yaml. Symbols = universe + watchlist + SPY (SPY for the validity engine later).
2. Daily closes: a year of split-adjusted closes per symbol, pulled once and topped up at
   most once a day (the "nightly" refresh), stored for good in data/terminal.db.
3. Fundamentals: Finnhub basic financials per symbol, cached a day.
4. Score everything: raw factors from the stored closes, fundamentals and DIRECT headline
   sentiment; z-scores against the universe; composite; labels by the universe's spread.
   The first board goes out here so the screen has labels within seconds of the data.
5. If universe news is on and a day old: pull Finnhub company news for every universe
   name so the news factor has a universe to compare with, then score again.
6. Record a signal for each watchlist ticker whose label changed (design call 1).
7. The options check per watchlist ticker (Phase 4): direction z-scores against the same
   universe, the contract list and chain snapshots, the risk-free rate from FRED, the next
   earnings date from Finnhub, today's ATM implied volatility stored for the IV rank, and
   a signal when a Call or Put candidate appears or changes contract.
Anything missing stays missing and is named in the score's notes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from ..cache import Cache
from ..clients.alpaca_client import AlpacaClient
from ..clients.finnhub_client import FinnhubClient
from ..clients.fred_client import FredClient
from ..datapoint import DataPoint, utcnow
from ..news.engine import NewsEngine
from ..news.models import KIND_NEWS
from ..news.store import NewsStore
from .factors import low_risk, mean_std, momentum, news_sentiment, quality_components, recent_news_sentiment, return_63, trend, value, vs_50d_average, zscore
from .options import CALL, CALL_CANDIDATE, NO_TRADE, PUT, PUT_CANDIDATE, DirectionScore, OptionsCheck, OptionsResult, contracts_from_payload
from .scoring import RawFactors, Score, score_all
from .backtest import params_for, run_backtest
from .calendar import Event, load_events
from .experiments import DeflatedSharpe, deflated_sharpe
from .signal_scorer import SignalScorer
from .sizing import Correlation, SizingRow, atr, contracts_for, correlation, shares_for
from .store import LONG_TERM, OPTIONS, MarketStore
from .validity import ValidityReport, build_report

Progress = Optional[Callable[[str], None]]


@dataclass
class QuantBoard:
    scores: dict[str, Score] = field(default_factory=dict)   # watchlist tickers only
    universe_name: str = ""
    universe_n: int = 0
    scored_n: int = 0
    as_of: Optional[str] = None
    run_at: Optional[datetime] = None
    status: str = ""                                         # one line for the Score tab
    notes: list[str] = field(default_factory=list)           # data problems at the universe level
    signals: list[str] = field(default_factory=list)         # label changes recorded this run
    complete: bool = True                                    # False while the universe-news pass is still to come
    options: dict[str, OptionsResult] = field(default_factory=dict)   # the call / put check per watchlist ticker
    earnings: dict[str, DataPoint] = field(default_factory=dict)      # next earnings date per watchlist ticker
    validity: dict[str, ValidityReport] = field(default_factory=dict) # "long_term" and "options" reports
    scored: list[str] = field(default_factory=list)                   # signals scored this run
    backtest: Optional[dict[str, Any]] = None                         # the latest backtest, as stored
    experiments: list[dict[str, Any]] = field(default_factory=list)   # the experiment log
    dsr: Optional[DeflatedSharpe] = None
    sizing: list[SizingRow] = field(default_factory=list)
    account: Optional[float] = None
    correlation: Optional[Correlation] = None
    events: list[Event] = field(default_factory=list)
    calendar_note: str = ""

    def validity_text(self, kind: str) -> str:
        report = self.validity.get(kind)
        return report.column_text if report is not None else "UNPROVEN 0/30"

    @property
    def virtual_mode(self) -> bool:
        return any(r.virtual_mode for r in self.validity.values())


def load_universe(path: str | Path) -> tuple[str, list[str]]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"universe file {p} not found")
    with p.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    tickers = [str(t).strip().upper() for t in (data.get("tickers") or []) if str(t).strip()]
    if len(tickers) < 5:
        raise ValueError(f"{p} lists {len(tickers)} tickers; the score needs at least 5")
    return str(data.get("name") or p.stem), list(dict.fromkeys(tickers))


class QuantEngine:
    def __init__(
        self,
        cfg: dict[str, Any],
        store: MarketStore,
        news_store: NewsStore,
        news_engine: Optional[NewsEngine],
        alpaca: AlpacaClient,
        finnhub: FinnhubClient,
        cache: Cache,
        logger: Optional[logging.Logger] = None,
        fred: Optional[FredClient] = None,
    ) -> None:
        self.cfg = cfg
        self.q = cfg["quant"]
        self.o = cfg["options"]
        self.fred = fred
        self.options_check = OptionsCheck(cfg)
        self.store = store
        self.news_store = news_store
        self.news_engine = news_engine
        self.alpaca = alpaca
        self.finnhub = finnhub
        self.cache = cache
        self.log = logger or logging.getLogger("stock_terminal.quant")

    # ----- data refresh -----
    def _marker_fresh(self, key: str) -> bool:
        entry = self.cache.get(key)
        return entry is not None and entry.is_fresh(self.cache.now())

    def _mark(self, key: str) -> None:
        self.cache.set(key, {"at": utcnow().isoformat()}, float(self.q["universe_ttl_seconds"]))

    def refresh_bars(self, symbols: list[str], now: datetime, progress: Progress = None, force: bool = False) -> list[str]:
        """Top up daily closes for the symbols. Returns the symbols whose pull failed."""
        # The daily marker only skips the top-up of names that are already complete; a name that was never pulled, was
        # pulled before highs and lows were stored, or is not deep enough for the backtest is pulled regardless
        # (seen live 2026-11-22: the marker hid the three-year re-pull for a whole day).
        checked_today = not force and self._marker_fresh("quant:bars_checked")
        history_days = max(int(self.q["history_days"]), int(self.cfg.get("backtest", {}).get("history_days", 0)))
        history_start = now - timedelta(days=history_days)
        fresh: list[str] = []
        stale: list[str] = []
        oldest_last: Optional[str] = None
        for symbol in symbols:
            last = self.store.last_bar_date(symbol)
            first = self.store.first_bar_date(symbol)
            if last is None or not self.store.has_ranges(symbol) or (first is not None and first > (history_start + timedelta(days=7)).date().isoformat()):
                fresh.append(symbol)   # never pulled, pulled before highs and lows were stored, or not deep enough for the backtest
            elif not checked_today and (force or last < (now - timedelta(days=1)).date().isoformat()):   # force: top up everything
                stale.append(symbol)
                oldest_last = last if oldest_last is None else min(oldest_last, last)
        failed: list[str] = []
        if fresh:
            if progress:
                progress(f"daily closes for {len(fresh)} new symbols")
            failed += self._store_closes(self.alpaca.daily_closes(fresh, history_start, now), fresh)
        if stale and oldest_last:
            if progress:
                progress(f"topping up daily closes for {len(stale)} symbols")
            start = datetime.fromisoformat(oldest_last).replace(tzinfo=now.tzinfo) - timedelta(days=3)
            failed += self._store_closes(self.alpaca.daily_closes(stale, start, now), stale)
        if not failed:
            self._mark("quant:bars_checked")
        return failed

    def _store_closes(self, dp: DataPoint, symbols: list[str]) -> list[str]:
        if not dp.ok:
            self.log.warning("daily closes unavailable: %s", dp.note)
            return list(symbols)
        failed = []
        for symbol in symbols:
            rows = dp.value.get(symbol) or []
            if not rows:
                failed.append(symbol)
                continue
            self.store.upsert_bars(symbol, [(r["t"], r["c"], r.get("h"), r.get("l")) for r in rows])
        return failed

    def refresh_metrics(self, symbols: list[str], progress: Progress = None) -> dict[str, DataPoint]:
        out: dict[str, DataPoint] = {}
        for i, symbol in enumerate(symbols, 1):
            out[symbol] = self.finnhub.metrics(symbol)
            if progress and (i % 10 == 0 or i == len(symbols)) and out[symbol].note != "cache":
                progress(f"fundamentals {i}/{len(symbols)}")
        return out

    # ----- factors -----
    def _direct_sentiments(self, symbols: list[str], tickers: list[str], now: datetime) -> dict[str, list[tuple[float, float]]]:
        """(age in days, sentiment) per symbol for scored, non-duplicate DIRECT headlines in the news window."""
        since = now - timedelta(days=float(self.q["news_window_days"]))
        rmap = self.news_engine.load_map() if self.news_engine else None
        buckets: dict[str, list[tuple[float, float]]] = {s: [] for s in symbols}
        ticker_set = set(tickers)
        for item in self.news_store.recent(since):
            if item.is_duplicate or item.sentiment is None or item.kind != KIND_NEWS:
                continue
            age = (now - item.published_at).total_seconds() / 86400.0
            hit = {s for s in item.symbols if s in buckets}
            if rmap is not None:
                hit |= {t for t in ticker_set if t not in hit and rmap.mentions(item.headline, t)}
            for symbol in hit:
                buckets[symbol].append((age, float(item.sentiment)))
        return buckets

    def raw_factors(self, symbols: list[str], tickers: list[str], metrics: dict[str, DataPoint], now: datetime) -> dict[str, RawFactors]:
        closes_by_symbol = self.store.closes_many(symbols, limit=int(self.q["history_days"]))
        sentiments = self._direct_sentiments(symbols, tickers, now)
        raws: dict[str, RawFactors] = {}
        for symbol in symbols:
            closes = closes_by_symbol.get(symbol, [])
            price = closes[-1][1] if closes else None
            as_of = closes[-1][0] if closes else None
            dp = metrics.get(symbol)
            metric_values = dp.value if dp is not None and dp.ok else None
            bars_source = f"alpaca/iex daily closes, split-adjusted, {len(closes)} days to {as_of}" if closes else "no daily closes stored"
            if dp is not None and dp.ok:
                when = dp.fetched_at.strftime("%b %d %H:%M UTC") if dp.fetched_at else "?"
                metrics_source = f"finnhub/metric (basic financials) fetched {when}, {dp.status}"
            else:
                metrics_source = f"finnhub/metric: {dp.note if dp is not None else 'not fetched'}"
            raws[symbol] = RawFactors(
                symbol=symbol,
                as_of=as_of,
                values={
                    "momentum": momentum(closes),
                    "trend": trend(closes),
                    "low_risk": low_risk(closes),
                    "value": value(metric_values, price),
                    "news": news_sentiment(sentiments.get(symbol, []), float(self.q["news_halflife_days"]), int(self.q["news_min_headlines"])),
                    # the options check's direction components, z-scored against the same universe
                    "ret_63": return_63(closes),
                    "vs_50d": vs_50d_average(closes),
                    "news_7d": recent_news_sentiment(sentiments.get(symbol, []), float(self.o["news_days"]), int(self.o["news_min_headlines"])),
                },
                quality=quality_components(metric_values),
                sources={
                    "momentum": bars_source, "trend": bars_source, "low_risk": bars_source,
                    "value": metrics_source, "quality": metrics_source,
                    "news": "DIRECT headlines in the news store (Alpaca and Finnhub), FinBERT sentiment",
                },
                price=price,
            )
        return raws

    # ----- the refresh -----
    def refresh(
        self,
        tickers: list[str],
        now: Optional[datetime] = None,
        on_board: Optional[Callable[[QuantBoard], None]] = None,
        progress: Progress = None,
        force: bool = False,
    ) -> QuantBoard:
        now = now or utcnow()
        tickers = [t.upper() for t in tickers]
        try:
            universe_name, members = load_universe(self.q["universe_path"])
        except Exception as exc:  # noqa: BLE001  a bad universe file is a status line, not a crash
            self.log.warning("universe unavailable: %s", exc)
            board = QuantBoard(status=f"no score: {exc}", run_at=now, notes=[str(exc)])
            if on_board:
                on_board(board)
            return board
        symbols = list(dict.fromkeys(members + tickers + ["SPY"]))
        scorable = [s for s in symbols if s != "SPY" or "SPY" in tickers or "SPY" in members]

        notes: list[str] = []
        failed_bars = self.refresh_bars(symbols, now, progress, force)
        if failed_bars:
            notes.append(f"daily closes missing for {len(failed_bars)}: {', '.join(failed_bars[:8])}{'...' if len(failed_bars) > 8 else ''}")
        metrics = self.refresh_metrics(scorable, progress)
        missing_metrics = [s for s, dp in metrics.items() if not dp.ok]
        if missing_metrics:
            reason = metrics[missing_metrics[0]].note
            notes.append(f"fundamentals missing for {len(missing_metrics)} ({reason}): {', '.join(missing_metrics[:8])}{'...' if len(missing_metrics) > 8 else ''}")

        wants_universe_news = bool(self.q["universe_news"]) and self.news_engine is not None and self.finnhub.enabled
        news_due = wants_universe_news and (force or not self._marker_fresh("quant:universe_news"))

        board = self._score(tickers, scorable, members, universe_name, metrics, now, notes, complete=not news_due, progress=progress)
        if on_board:
            on_board(board)
        if not news_due:
            return board

        if progress:
            progress("pulling universe news for the news factor")
        others = [m for m in members if m not in tickers]
        result = self.news_engine.ingest_company_news(others, now - timedelta(days=float(self.q["news_window_days"])), now, progress)
        self.log.info("universe news: %s", result)
        if result["failed"]:
            notes.append(f"universe news failed for {len(result['failed'])}: {result['failed'][0]}")
        else:
            self._mark("quant:universe_news")
        board = self._score(tickers, scorable, members, universe_name, metrics, now, notes, complete=True, progress=progress)
        if on_board:
            on_board(board)
        return board

    def _score(
        self, tickers: list[str], scorable: list[str], members: list[str], universe_name: str,
        metrics: dict[str, DataPoint], now: datetime, notes: list[str], complete: bool, progress: Progress = None,
    ) -> QuantBoard:
        raws = self.raw_factors(scorable, tickers, metrics, now)
        scores = score_all(
            raws, members, self.q["weights"], float(self.q["zscore_cap"]), float(self.q["label_top_pct"]), float(self.q["label_bottom_pct"]),
            clip_pct=float(self.q.get("quality_clip_pct", 0)),
        )
        self.store.save_scores(now, [s.to_row() for s in scores.values()])
        events: list[str] = []
        for ticker in tickers:
            score = scores.get(ticker)
            if score is None or score.label is None:
                continue
            previous = self.store.open_signal(ticker)
            signal = self.store.record_label(ticker, score.label, score.price, now, int(self.q["signal_horizon_days"]))
            if signal is not None:
                before = previous.label if previous else "none"
                events.append(f"{ticker}: {before} -> {score.label}")
                self.log.info("signal: %s label %s -> %s at %s", ticker, before, score.label, score.price)
        options: dict[str, OptionsResult] = {}
        earnings: dict[str, DataPoint] = {}
        if bool(self.o["enabled"]):
            direction_stats = self._direction_stats(raws, set(members))
            for ticker in tickers:
                raw = raws.get(ticker)
                if raw is None:
                    continue
                earnings[ticker] = self.finnhub.next_earnings(ticker, now.date())
                result = self._options_for(ticker, raw, direction_stats, earnings[ticker], now)
                options[ticker] = result
                events.extend(self._log_options_signal(ticker, result, raw.price, now))
        # The honesty check: score what is due, then the three tests per signal kind.
        scorer = SignalScorer(self.store, members, int(self.q["history_days"]), as_of=now.date())
        scored_events = scorer.score_due(now)
        validity = {
            kind: build_report(kind, scorer.scored(kind), self.store.open_count(kind), self.cfg["validity"], scorer.gap_outcome)
            for kind in (LONG_TERM, OPTIONS)
        }
        backtest, experiments, dsr = self._backtest(members, now, progress, run=complete)
        sizing, account, corr = self._sizing(tickers, raws, options)
        calendar_events, calendar_note = load_events(self.cfg.get("calendar_path", "calendar.yaml"))
        any_score = next(iter(scores.values()), None)
        scored_n = any_score.scored_n if any_score else 0
        as_of = max((s.as_of for s in scores.values() if s.as_of), default=None)
        if scored_n == 0:
            status = "no score: nothing in the universe could be scored" + (f" ({notes[0]})" if notes else "")
        else:
            status = f"scored {scored_n} of {len(members)} {universe_name} names, closes to {as_of}"
            if not complete:
                status += "; news factor fills after the universe news pull"
        return QuantBoard(
            scores={t: scores[t] for t in tickers if t in scores},
            universe_name=universe_name, universe_n=len(members), scored_n=scored_n, as_of=as_of, run_at=now,
            status=status, notes=list(notes), signals=events, complete=complete, options=options, earnings=earnings,
            validity=validity, scored=scored_events, backtest=backtest, experiments=experiments, dsr=dsr,
            sizing=sizing, account=account, correlation=corr, events=calendar_events, calendar_note=calendar_note,
        )

    # ----- the backtest and the experiment log -----
    def _backtest(self, members: list[str], now: datetime, progress: Progress, run: bool) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]], Optional[DeflatedSharpe]]:
        b = self.cfg["backtest"]
        params, digest = params_for(self.cfg)
        latest = self.store.latest_backtest()
        current = latest["result"] | {"run_at": latest["run_at"].isoformat() if latest["run_at"] else None, "params_hash": latest["params_hash"]} if latest else None
        stale = latest is None or latest["params_hash"] != digest or not self._marker_fresh("quant:backtest")
        if bool(b["enabled"]) and run and stale:
            if progress:
                progress("backtest: replaying the price factors")
            try:
                result = run_backtest(self.store, members, self.cfg, now.date(), progress, run_at=now)
            except Exception as exc:  # noqa: BLE001  a backtest bug is a status line, never a crash of the live screen
                self.log.exception("backtest failed")
                current = {"signals": 0, "notes": [f"backtest failed: {exc}"]}
            else:
                current = result.to_dict() | {"run_at": result.run_at.isoformat(), "params_hash": digest}
                self.store.save_backtest(now, params, digest, result.to_dict())
                new_trial = False
                if result.signals:   # a replay that produced nothing is not a trial
                    new_trial = self.store.record_experiment(now, digest, params, result.sharpe_per_period, len(result.period_returns), result.skew, result.kurtosis)
                    self.cache.set("quant:backtest", {"at": now.isoformat()}, float(b["ttl_seconds"]))
                self.log.info("backtest: %d labels, %d wins, status %s, trial %s%s", result.signals, result.wins, result.report.status if result.report else "-", digest, " (new trial)" if new_trial else "")
        experiments = self.store.experiments()
        return current, experiments, deflated_sharpe(experiments, digest)

    # ----- sizing and correlation (display only; never touch a signal) -----
    def _sizing(self, tickers: list[str], raws: dict[str, RawFactors], options: dict[str, OptionsResult]) -> tuple[list[SizingRow], Optional[float], Optional[Correlation]]:
        s = self.cfg["sizing"]
        raw_account = self.store.setting("sizing:account")
        try:
            account = float(raw_account) if raw_account else None
        except ValueError:
            account = None
        rows: list[SizingRow] = []
        for ticker in tickers:
            raw = raws.get(ticker)
            price = raw.price if raw else None
            bars = self.store.bars(ticker, int(s["atr_sessions"]) + 5)
            a = atr(bars, int(s["atr_sessions"]))
            note = "" if a is not None else f"ATR needs {int(s['atr_sessions']) + 1} sessions with highs and lows"
            result = options.get(ticker)
            ask = result.contract.ask if result is not None and result.contract is not None else None
            stop = float(s["atr_multiple"]) * a if a is not None else None
            rows.append(SizingRow(
                ticker=ticker, price=price, atr=a, stop_distance=stop,
                shares=shares_for(account, float(s["risk_pct"]), a, float(s["atr_multiple"])) if account else None,
                ask=ask, contracts=contracts_for(account, float(s["risk_pct"]), ask) if account else None, note=note,
            ))
        corr = correlation(self.store.closes_many(tickers, int(s["correlation_sessions"]) + 10), int(s["correlation_sessions"]), float(s["correlation_warn"])) if len(tickers) >= 2 else None
        return rows, account, corr

    def set_account(self, value: float) -> None:
        self.store.set_setting("sizing:account", f"{float(value):.2f}")

    # ----- the options check -----
    def _direction_stats(self, raws: dict[str, RawFactors], members: set[str]) -> dict[str, tuple[Optional[float], Optional[float], int]]:
        stats = {}
        for name in ("ret_63", "vs_50d", "news_7d"):
            values = [r.values[name].value for s, r in raws.items() if s in members and name in r.values and r.values[name].ok]
            avg, sd = mean_std(values)
            stats[name] = (avg, sd, len(values))
        return stats

    def _direction(self, raw: RawFactors, stats: dict) -> DirectionScore:
        components: dict[str, dict[str, Any]] = {}
        zs = []
        for name, title in (("ret_63", "63-day return"), ("vs_50d", "price vs 50-day average"), ("news_7d", "7-day DIRECT sentiment")):
            fv = raw.values.get(name)
            avg, sd, n = stats.get(name, (None, None, 0))
            z = zscore(fv.value if fv else None, avg, sd, float(self.q["zscore_cap"]))
            components[title] = {
                "raw": fv.value if fv else None, "z": z, "mean": avg, "std": sd, "n": n,
                "note": (fv.note if fv and not fv.ok else ("" if z is not None else f"no universe spread (n={n})")),
            }
            if z is not None:
                zs.append(z)
        if len(zs) < 2:
            return DirectionScore(None, components, float(self.o["direction_threshold"]), f"only {len(zs)} of 3 components available; needs 2")
        note = "" if len(zs) == 3 else "2 of 3 components available"
        return DirectionScore(sum(zs) / len(zs), components, float(self.o["direction_threshold"]), note)

    def _options_for(self, ticker: str, raw: RawFactors, stats: dict, earnings: DataPoint, now: datetime) -> OptionsResult:
        direction = self._direction(raw, stats)
        kind = direction.side or CALL
        price = raw.price
        contracts: list = []
        status = DataPoint.missing("alpaca/option-contracts", "no price")
        if price:
            lo, hi = now.date() + timedelta(days=int(self.o["dte_min"])), now.date() + timedelta(days=int(self.o["dte_max"]))
            window = float(self.o["strike_window_pct"]) / 100.0
            status = self.alpaca.option_contracts(ticker, lo, hi, kind, price * (1 - window), price * (1 + window))
            if status.ok:
                chain = self.alpaca.option_chain(ticker, lo, hi, kind, price * (1 - window), price * (1 + window))
                contracts = contracts_from_payload(status.value, chain.value if chain.ok else None)
                if not chain.ok:
                    status = DataPoint.missing(chain.source, f"chain snapshots: {chain.note}")
        rate = self.fred.latest(str(self.o["rate_series"])) if self.fred is not None else DataPoint.missing("fred", "no FRED client")
        closes = self.store.closes(ticker, limit=int(self.q["history_days"]))
        # IV rank uses the days stored before today; today's value is stored after the check runs.
        iv_now = None
        preview = self.options_check.run(ticker, price, closes, direction, contracts, status, rate, earnings, (None, 0), now)
        iv_now = preview.metrics.get("iv")
        rank = self.store.iv_rank(ticker, iv_now)
        result = preview
        result.metrics["iv_rank"], result.metrics["iv_days"] = rank
        if iv_now is not None and result.contract is not None:
            self.store.record_iv(ticker, now.date().isoformat(), iv_now, result.contract.symbol)
            result.metrics["iv_days"] = self.store.iv_rank(ticker, iv_now)[1]
        return result

    def _log_options_signal(self, ticker: str, result: OptionsResult, price: Optional[float], now: datetime) -> list[str]:
        events: list[str] = []
        if result.label in (CALL_CANDIDATE, PUT_CANDIDATE) and result.contract is not None:
            previous = self.store.open_signal(ticker, OPTIONS)
            signal = self.store.record_label(ticker, result.label, price, now, int(result.metrics.get("dte") or 1), OPTIONS, detail=result.contract.symbol, target=result.metrics.get("breakeven"))
            if signal is not None:
                before = f"{previous.label} ({previous.note})" if previous else "none"
                events.append(f"{ticker} options: {before} -> {result.label} ({result.contract.symbol})")
                self.log.info("signal: %s options %s -> %s %s at %s", ticker, before, result.label, result.contract.symbol, price)
        else:
            closed = self.store.close_open(ticker, OPTIONS, price, now, f"label changed to {NO_TRADE}")
            if closed is not None:
                events.append(f"{ticker} options: {closed.label} -> {NO_TRADE}")
                self.log.info("signal: %s options %s closed: No trade at %s", ticker, closed.label, price)
        return events

# Stock Terminal

A keyboard-driven terminal app for a watchlist of stocks. Three jobs, built in six phases:

1. **News** for each stock, plus news that moves it without naming it (Phase 2).
2. **Quant read**: a long-term score and a call / put check, every formula and input on screen (Phases 3 and 4).
3. **Honesty check**: every label the app produces is logged and tested against chance with the methods in
   Berdondini's *The Theory of Quantitative Trading* (Phases 5 and 6).

**Read-only.** This program places no orders and contains no trading code. It never says "buy" or "sell":
labels are Favorable / Neutral / Unfavorable and Call candidate / Put candidate / No trade, always shown with a
validity status. The full spec is in [docs/SPEC.md](docs/SPEC.md).

## Status

| Phase | What | State |
|---|---|---|
| 1 | Skeleton: screen, watchlist, prices, market clock, VIX and 10-year, config, keys, cache, rate limits, log panel, tests | **Built 2026-11-15, live data confirmed 2026-11-18 (Alpaca) and 2026-11-22 (FRED).** |
| 2 | News engine: relationship map, Alpaca + Finnhub + EDGAR, DIRECT / PEER / SECTOR / MACRO / FILING tags, catalyst types, FinBERT sentiment, dedupe, reaction tracker, news pane | **Built 2026-11-22, live on every source the same night:** Alpaca news, Finnhub, EDGAR, FinBERT, reactions filled from bars. |
| 3 | Long-term score with the explain view | Not started |
| 4 | Options check | Not started |
| 5 | Signal log, three validity tests, status banner | Not started |
| 6 | Backtest, experiment log, sizing panel, extras | Not started |

## Run it

PowerShell, from this folder. The virtual environment (`.venv`, a private copy of Python with this project's
packages) is already built on this machine.

```powershell
cd "C:\Users\tyler\Claude Projects\stock-terminal"
.venv\Scripts\python -m stock_terminal
```

Check every data source once without opening the screen (the first run downloads the sentiment model, about 440 MB):

```powershell
.venv\Scripts\python -m stock_terminal --check
```

Print one ticker's tagged news feed, Finnhub's peer list as a snippet for `relationships.yaml`, or the reaction table:

```powershell
.venv\Scripts\python -m stock_terminal --news AAPL
.venv\Scripts\python -m stock_terminal --peers AAPL
.venv\Scripts\python -m stock_terminal --reactions
```

Run the tests:

```powershell
.venv\Scripts\python -m pytest -q
```

First-time setup on a new machine (not needed here):

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install -e .
Copy-Item .env.example .env
```

Then put the keys in `.env`.

## Keys

Values go in `.env` only. `.env` is gitignored and the app never prints or logs a key. `.env.example` lists the
names. The safest way to add a line is a command that asks for the value, so nothing in the command needs editing:

```powershell
$k = Read-Host "Paste the Finnhub key"; Add-Content .env "FINNHUB_API_KEY=$k"
```

| Variable | Service | Where it comes from |
|---|---|---|
| `ALPACA_PAPER_KEY_ID`, `ALPACA_PAPER_SECRET_KEY` | Alpaca | The paper-trading keys. Paper keys serve the free market-data plan. Same names as the trading-bot repo on purpose. Prices, bars and news. |
| `FRED_API_KEY` | FRED | Free account at https://fred.stlouisfed.org, then My Account, API Keys. |
| `FINNHUB_API_KEY` | Finnhub | Free account at https://finnhub.io/register. Company news, general news, peers. |
| `EDGAR_USER_AGENT` | SEC EDGAR | Not a key: `stock-terminal <your email>`, required by the SEC on every request. 8-K filings and Form 4 insider trades. Without it, FILING items are missing with that reason. |

## The screen

```
SPY 693.89 (-3.07%)   QQQ 614.80 (-4.70%)   VIX 28.44 (Nov 19)   10Y 5.04% (Nov 19)
Market CLOSED, opens Mon Nov 23 9:30 AM ET | Newest IEX trade 2d ago | Refreshed ... | * stale  ~ delayed  -- missing
+----------------------------------------------+-------------------------------+-----------------------+
| Ticker Price   Chg %  LT  Opt Earn Validity  | AAPL news  filter ALL  23 of 23| Score | Options | Validity |
| AAPL   343.40  -1.63% --  --  --   UNPROVEN  | sentiment ok (ProsusAI/finbert)|                       |
| MSFT   ...                                   |  2h DIRECT earnings    sent +0.62                      |
|                                              | Apple reports record quarterly earnings                |
|                                              | benzinga  reaction +1h +0.5%  +1d ...  +5d ...         |
+----------------------------------------------+-------------------------------+-----------------------+
> Log (l to toggle)
```

| Key | Does |
|---|---|
| `a` | add a ticker (type it, Enter; Esc cancels) |
| `d` | delete the highlighted ticker |
| `r` | refresh prices and news now (prices also refresh every `refresh_seconds`, news every `news.refresh_seconds`) |
| `f` | cycle the news filter: ALL, DIRECT, PEER, SECTOR, MACRO, FILING |
| `o` | open the highlighted headline in the browser (Tab into the news list, arrows to pick one) |
| `?` | explain view: arrives in Phase 3 with the score |
| `l` | show or hide the log panel |
| `q` | quit |

How to read a number:

- A plain number is fresh. `*` means stale: the source failed and this is the last value we had. `~` means delayed:
  the trade is older than 15 minutes while the market is open. `--` means missing. The Data column carries the one-word
  status (`ok`, `STALE`, `DELAYED`, `MISSING`) and the top strip's `Missing:` note says why (for example
  `no Alpaca key in .env`). Nothing is ever filled in with a placeholder.
- Prices come from Alpaca's IEX feed: one exchange, real-time, free. The official consolidated price can differ by
  a cent or two.
- `Chg %` is the change against the previous session's close.
- VIX and 10Y come from FRED, which posts each value the next business day, so the strip shows the previous close
  with its date.
- The validity column reads `UNPROVEN 0/30` until the app has produced and scored 30 signals (Phase 5).

## The news pane

The middle pane is the feed for the highlighted ticker, newest first. Each headline takes three lines:

1. its age, its **relation tag**, its **catalyst type**, and its **sentiment**;
2. the headline;
3. the outlet and the **reaction** so far.

**Relation tag**, worked out per ticker from `relationships.yaml`, in this order of precedence:

| Tag | Means |
|---|---|
| `FILING` | an 8-K or Form 4 from EDGAR for this company |
| `DIRECT` | the source attached this ticker, or the headline names the company (its `aliases`) |
| `PEER` | same, for one of the ticker's `peers` |
| `SECTOR` | the ticker's `sector_etf` is attached, or the headline uses that sector's keywords |
| `MACRO` | general news whose words match one of the ticker's `topics` (rates, tariffs, chip export rules and so on) |

The same headline is `DIRECT` for MSFT and `PEER` for AAPL. A headline that matches none of these for a ticker is not
shown for it. A ticker with no entry in `relationships.yaml` gets `DIRECT` and `FILING` only, and the pane says so;
`--peers TICKER` prints Finnhub's peer list as a block to paste into the file.

**Catalyst type**: earnings, guidance, analyst rating, M&A, legal/regulatory, product, insider, macro, or other. It is
the first type in the `catalysts` table of `relationships.yaml` with a keyword in the headline, so every tag can be
traced to a word. Filings bring their own: an 8-K with item 2.02 is earnings, a Form 4 is insider.

**Sentiment** is FinBERT's P(positive) minus P(negative), from -1 to +1, on the headline alone. The model
(ProsusAI/finbert, licence CC BY-NC-SA 4.0, personal use) runs on the CPU and is downloaded once by Hugging Face
into your user profile. When it is not installed or fails to load, every sentiment shows `--` and the pane's second
line says why. Set `sentiment.enabled: false` in `config.yaml` to skip it.

**Duplicates**: two headlines within 48 hours that share at least half their words (after dropping filler words), or
that point at the same link, are one story; the earliest is shown, the rest are kept in the database but hidden.
Different articles about the same event are not merged on purpose.

**Reaction**: for every headline shown for a ticker, the app records the first traded price at or after the headline
(a headline that lands after the close gets the next open), then the price one hour after that, and the closes of the
first and fifth sessions after it. The prices come from Alpaca bars, so they fill in even if the app was closed, and a
slot shows `...` until its time has passed and a bar exists. `--reactions` prints the average move by ticker, tag and
catalyst with the number of headlines behind each number: that is the table of how much each kind of news really moves
each stock, built from your own watchlist over time.

Sources and limits: Alpaca news (50 newest per call; one call per ticker group plus one for the market symbols in
`config.yaml` and the sector ETFs, which is where Fed, tariff and sector stories are filed), Finnhub
company news (one call per ticker) and general news, EDGAR 8-K and Form 4 for the last 30 days (one filing list per
ticker per hour; the newest ten Form 4 XMLs are read for who bought or sold what). News refreshes every ten minutes
by default; every source has its own rate limiter under the published limit and a failing source shows as a status
line in the pane, never as a crash.

This product uses the FRED API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.

## How the code is laid out

```
src/stock_terminal/
  __main__.py         entry point: the screen, or --check / --news / --peers / --reactions
  app.py              the Textual screen, keys, rendering
  provider.py         refresh() for prices (Board) and refresh_news() for the feeds (NewsBoard)
  relationships.py    relationships.yaml: per-ticker map and the keyword tables
  clients/base.py     the shared fetch routine: cache, rate limit, stale or missing on failure
  clients/alpaca_client.py   stock snapshots, the market clock, minute and daily bars
  clients/alpaca_news.py     Alpaca news
  clients/finnhub_client.py  Finnhub company news, general news, peers
  clients/edgar_client.py    EDGAR filing lists, 8-K items, Form 4 XML parsing
  clients/fred_client.py     VIX and the 10-year yield
  news/models.py      NewsItem, the relation tag per ticker, Reaction, the feed shapes
  news/engine.py      one refresh: pull, tag, store, score, schedule and fill reactions, build feeds
  news/store.py       SQLite tables news and reactions (same file as the cache)
  news/dedupe.py      the same-story rule
  news/sentiment.py   FinBERT behind a swappable Scorer interface
  news/reactions.py   the rules for filling a reaction from bars
  datapoint.py        DataPoint: a value plus its source, age and status
  cache.py            SQLite cache (data/terminal.db)
  ratelimit.py        rolling-window rate limiter, one per client
  config.py           config.yaml with defaults and validation; Keys from .env (repr hides values)
  watchlist.py        watchlist.yaml, add / remove / save
  logging_setup.py    rotating log file (logs/terminal.log) and the hook for the log panel
tests/                pytest; no network, fakes for every client and the sentiment model
docs/SPEC.md          the spec
relationships.yaml    the relationship map and keyword tables (edit freely)
```

Rules the code keeps: no order code anywhere; every outside call goes through a client with a rate limiter and the
cache so one failing source never crashes the app; a missing or stale value is labelled, never invented; keys are
read once from the environment and never shown.

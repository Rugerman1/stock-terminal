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
| 1 | Skeleton: screen, watchlist, prices, market clock, VIX and 10-year, config, keys, cache, rate limits, log panel, tests | **Built 2026-11-15.** Runs with no keys (every number shows as missing with the reason). Live data not yet confirmed by Tyler. |
| 2 | News engine: tags, FinBERT sentiment, dedupe, filings, reaction tracker | Not started |
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

Check every data source once without opening the screen:

```powershell
.venv\Scripts\python -m stock_terminal --check
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
names.

| Variable | Service | Where it comes from |
|---|---|---|
| `ALPACA_PAPER_KEY_ID`, `ALPACA_PAPER_SECRET_KEY` | Alpaca | The paper-trading keys. Paper keys serve the free market-data plan. Same names as the trading-bot repo on purpose. |
| `FRED_API_KEY` | FRED | Free account at https://fred.stlouisfed.org, then My Account, API Keys. |
| `FINNHUB_API_KEY` | Finnhub | Free account at https://finnhub.io/register. Used from Phase 2. |
| `EDGAR_USER_AGENT` | SEC EDGAR | Not a key: `stock-terminal <contact email>`, required by the SEC. Used from Phase 2. |

## The screen

```
SPY 672.10 (+0.41%)   QQQ 600.20 (+0.55%)   VIX 18.70 (Nov 12)   10Y 5.10% (Nov 12)
Market CLOSED, opens Mon Nov 16 9:30 AM ET | Newest IEX trade 2d ago | Refreshed ... | * stale  ~ delayed  -- missing
+----------------------------------------------+-----------------------+-----------------------+
| Ticker Price   Chg %  LT  Opt Earn Validity  | News (Phase 2)        | Score | Options | Validity |
| AAPL   272.41  +0.80% --  --  --   UNPROVEN  |                       |                       |
+----------------------------------------------+-----------------------+-----------------------+
> Log (l to toggle)
```

| Key | Does |
|---|---|
| `a` | add a ticker (type it, Enter; Esc cancels) |
| `d` | delete the highlighted ticker |
| `r` | refresh now (it also refreshes every `refresh_seconds` from `config.yaml`) |
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

This product uses the FRED API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.

## How the code is laid out

```
src/stock_terminal/
  __main__.py         entry point: the screen, or --check
  app.py              the Textual screen, keys, rendering
  provider.py         one refresh() that returns everything the screen needs (Board)
  clients/base.py     the shared fetch routine: cache, rate limit, stale or missing on failure
  clients/alpaca_client.py   stock snapshots and the market clock
  clients/fred_client.py     VIX and the 10-year yield
  datapoint.py        DataPoint: a value plus its source, age and status
  cache.py            SQLite cache (data/terminal.db)
  ratelimit.py        rolling-window rate limiter, one per client
  config.py           config.yaml with defaults and validation; Keys from .env (repr hides values)
  watchlist.py        watchlist.yaml, add / remove / save
  logging_setup.py    rotating log file (logs/terminal.log) and the hook for the log panel
tests/                pytest; no network, fakes for Alpaca and FRED
docs/SPEC.md          the spec
```

Rules the code keeps: no order code anywhere; every outside call goes through a client with a rate limiter and the
cache so one failing source never crashes the app; a missing or stale value is labelled, never invented; keys are
read once from the environment and never shown.

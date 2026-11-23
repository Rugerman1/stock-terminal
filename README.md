# Project Watchtower

A keyboard-driven terminal app for a watchlist of stocks: it watches, it never fires. Three jobs, built in six
phases (the Python package and this repo keep the working name `stock-terminal`):

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
| 3 | Long-term score: six factors as z-scores against the S&P 100, Favorable / Neutral / Unfavorable, the `?` explain view, the signal log | **Built 2026-11-22.** |
| 4 | Options check: direction score, contract pick on the indicative feed, HV and IV, vol ratio, IV rank collection, expected move, breakeven, two probabilities of profit, simulated expected value, max loss, Greeks, Call / Put candidate or No trade with the failed check named, earnings warning, options signal log | **Built 2026-11-22.** |
| 5 | Signal scoring at the horizon (beat SPY, or beyond breakeven), the binomial test at 50 percent and at the universe's rate, Monte Carlo, the Binomial Evolution Function per ticker, UNPROVEN / RANDOM-LIKE / WEAK EVIDENCE / SIGNIFICANT everywhere a label shows, the virtual-mode banner, `--validity` | **Built 2026-11-22.** Reads UNPROVEN until 30 signals are scored, which takes months by design. |
| 6 | Walk-forward backtest on the price factors feeding the same three tests, experiment log with the Deflated Sharpe Ratio, position sizing panel, watchlist correlation table, Fed / CPI / earnings dates strip, `b` brief, `--backtest`, `--size`, `--brief` | **Built 2026-11-22.** |

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

Print one ticker's long-term score with the full explain view, force the nightly universe pull now, or list the
signal log:

```powershell
.venv\Scripts\python -m stock_terminal --score AAPL
.venv\Scripts\python -m stock_terminal --options AAPL
.venv\Scripts\python -m stock_terminal --universe
.venv\Scripts\python -m stock_terminal --signals
.venv\Scripts\python -m stock_terminal --validity
.venv\Scripts\python -m stock_terminal --backtest
.venv\Scripts\python -m stock_terminal --size 25000
.venv\Scripts\python -m stock_terminal --brief
```

Recompute the news store's duplicate marks after changing the dedupe rule in `config.yaml`:

```powershell
.venv\Scripts\python -m stock_terminal --rededupe
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
| `?` | explain view for the highlighted ticker: the score and the options check, every formula, input and source (Esc closes) |
| `b` | write today's brief, a markdown summary of the whole screen, to the `briefs` folder |
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
- `LT` is the long-term label: `FAV` Favorable, `NEU` Neutral, `UNF` Unfavorable, `--` no score, `...` still computing. The
  Score tab spells it out.
- `Opt` is the options label: `CALL` Call candidate, `PUT` Put candidate, `none` No trade, `--` no check. The Options tab
  shows the contract, the numbers and which check failed. `Earn` is days to the next earnings date from Finnhub's calendar.
- The Validity column is the honesty check's verdict on the long-term labels: `UNPROVEN n/30` until 30 signals are scored,
  then `RANDOM-LIKE`, `WEAK EVIDENCE` or `SIGNIFICANT`. In RANDOM-LIKE the LT and Opt cells are greyed out and a red banner
  says so: virtual mode, keep recording, stop trusting. The Validity tab has the three tests for both kinds of signal.

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

**Duplicates**: two headlines that point at the same link, or that were published within 48 hours, are about a
company in common and share at least 70 percent of their words (after dropping filler words), are one story; the
earliest is shown and carries every company the copies were filed under, the rest are kept in the database but
hidden. Different articles about the same event are not merged on purpose, and "Why Apple Stock Rose Today" is not
"Why Nvidia Stock Rose Today". `--rededupe` recomputes the marks after a rule change.

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

## The long-term score

The Score tab shows the highlighted ticker's label, composite, percentile in the universe and one line per factor;
`?` opens the full explain view. Every number traces to a formula in `src/stock_terminal/quant/factors.py` and to a
source named on screen.

**Universe.** `universe.yaml` lists the reference group (the S&P 100 as of September 2026, 101 names, edit freely).
Every factor is a **z-score** against it: how many standard deviations the stock sits from the group's average,
capped at -3 and +3. The universe's closes, fundamentals and news are pulled once a day (the "nightly" refresh, on
the first score refresh after `quant.universe_ttl_seconds`; `--universe` forces it) and the closes are kept for good
in `data/terminal.db`. The first pull takes a few minutes: a year of daily closes for every name in one Alpaca call,
then Finnhub fundamentals and company news for each name, rate-limited under the free plan. The screen shows
labels as soon as the closes and fundamentals are in, and fills the news factor after the universe news pass.

| Factor | Formula | Weight |
|---|---|---|
| momentum | price 21 trading days ago / price 252 trading days ago - 1 | 0.30 |
| trend | price / 200-day simple average - 1 | 0.10 |
| value | average of earnings yield (EPS TTM / price) and free-cash-flow yield (1 / price-to-FCF per share TTM) | 0.20 |
| quality | average of the z-scores of ROE, gross margin and negative debt-to-equity | 0.25 |
| low risk | negative of 1-year realized volatility (sample std of 252 daily log returns x sqrt 252) | 0.10 |
| news | time-weighted DIRECT headline sentiment, weight 0.5 ^ (age in days / 3), needs 5 or more headlines | 0.05 |

Composite = sum of weight x z. A missing factor counts as z = 0 and is named on screen. Top 20 percent of the
universe by composite = Favorable, bottom 20 percent = Unfavorable, the rest Neutral. The weights and the 20 percent
cut-offs live in `config.yaml` and are starting guesses from the spec, never tuned to past data.

Two choices the spec left open, both shown in the explain view: quality's three parts have different units (percent,
percent, ratio), so each is z-scored against the universe before averaging, and each is clipped at the universe's 5th
and 95th percentile before the mean and std are taken (`quant.quality_clip_pct`, 0 turns it off), because a few
near-zero-equity names (ROE of 575 percent, debt-to-equity of 81) otherwise stretch the spread until everyone else's z
collapses toward zero; a clipped name says so in the explain view. Value's two parts are both yields, so they average
as written, except for the banks listed under `financials` in `relationships.yaml`, which use earnings yield alone:
a bank's free cash flow counts deposit inflows and reads as a 20 to 36 percent yield, which is not what the factor
measures; the explain view says "FCF yield left out" on those names. Prices are split-adjusted; fundamentals are Finnhub's trailing-twelve-month figures as of
the fetch time shown. A yield beyond 100 percent (earnings or cash flow per share larger than the price) is a data
error, not a bargain, and is rejected with a note: Finnhub reports Berkshire's Class A earnings per B share. Free cash
flow yield is not meaningful for banks, so their value factor leans on an inflated number; the spec's formula is kept
and the explain view shows the inputs.

**Signal log.** A label is logged only when it changes (one open long-term signal per ticker, so a stock that stays
Favorable for three months is one bet, not sixty), with the close at the time and a 63-trading-day horizon. Phase 5
scores each signal against SPY when its horizon passes. `--signals` prints the log.

## The options check

The Options tab runs the spec's four steps for the highlighted ticker every 15 minutes and `?` explains each number.
Read-only: quotes and Greeks are looked at, never traded, and the "contract" is a name to study, not an order.

1. **Direction**: the average of three z-scores against the universe: 63-day return, price / 50-day average - 1, and
   the plain average sentiment of the last 7 days of DIRECT headlines (needs 3). Beyond +1 looks at calls, beyond -1
   at puts, in between is No trade before any contract is touched. The at-the-money call is still analysed so IV, HV
   and the IV rank show.
2. **Contract**: 30 to 60 days to expiry, delta nearest 0.50, open interest 500 or more, bid-ask spread at most 10
   percent of the mid. Contracts and open interest come from Alpaca's contract list; quotes, implied volatility and
   Greeks from the chain snapshot on the free indicative feed (derived quotes, trades 15 minutes delayed, said so on
   screen). When no contract passes liquidity, the nearest-delta one is shown and the liquidity check fails.
3. **Numbers**: HV20 and HV60 (standard deviation of daily log returns x sqrt 252); the feed's IV, or Black-Scholes
   solved from the mid when the feed has none; vol ratio IV / HV60; IV rank from one at-the-money IV stored per day
   (reads "collecting, n/252" until a year is in, because Alpaca has no historical IV to backfill); expected move
   price x IV x sqrt(days / 365); breakeven strike plus (call) or minus (put) the ask and the move it needs; probability
   of profit two ways, the market's (lognormal with IV and the risk-free rate as drift) and the model's (lognormal with
   HV60, zero drift); expected value as the average payoff over 20,000 simulated end prices (HV60, zero drift, fixed
   seed) minus the ask, x 100; max loss ask x 100; the feed's Greeks or Black-Scholes ones. The risk-free rate is
   FRED's 3-month Treasury.
4. **Label**: Call candidate or Put candidate only when direction, vol ratio (1.2 or less), the needed move within the
   expected move, and liquidity all pass; otherwise No trade with every failed check named. Earnings before expiry
   adds a warning that IV usually drops right after.

A candidate is logged as an options signal with the close and the contract's expiry as its horizon; a changed
contract or a return to No trade closes it. Phase 5 scores these too. `config.yaml`'s `options:` block holds every
threshold; they are the spec's numbers, not tuned.

## The honesty check

Every label is logged when it appears (Phases 3 and 4) and scored once its horizon passes: the 63rd session after a
long-term label, the contract's expiry for an options candidate, or the session the label changed on when that comes
first, because the label changing is the system closing the bet. A long-term label wins when the stock beat SPY in the
predicted direction over the window, so a rising market does not count as skill. An options candidate wins when the
stock ended beyond the breakeven on the predicted side. Neutral labels predict nothing and are skipped. Prices are the
stored daily closes; nothing is estimated between sessions.

The last 50 scored signals of each kind then go through the three tests from Berdondini's *The Theory of Quantitative
Trading* (pages 36 to 42 and 51 to 58):

1. **Binomial**: P(X >= k), the chance of k or more wins in n tries by luck, at the 50 percent baseline the spec sets for
   beat-SPY outcomes, and beside it at the empirical rate: how often a random universe name won over the same windows.
2. **Monte Carlo**: 10,000 runs; each run replaces every real signal with a random universe name over the same window and
   direction, scored the same way; the share of runs with at least as many wins.
3. **Binomial Evolution Function**, per ticker: insert an opposite test trade in each gap between two same-side trades
   (scored from the market's move over the gap), keep only the trades that sit in a consecutive same-result pair, binomial
   at 50 percent. The spec's example [1, 0, 1, 0, 1, 1, 0] gives 2 kept, 2 wins, 25 percent, and the test suite pins it.

Status: UNPROVEN under 30 scored; RANDOM-LIKE when the pooled binomial probability is above 5 percent (the book's virtual
mode: the app keeps recording, you stop trusting); WEAK EVIDENCE between 1 and 5 percent; SIGNIFICANT below 1 percent.
The headline probability is the pooled binomial at 50 percent; the other tests sit next to it. The thresholds live in
`config.yaml`'s `validity:` block. The spec's binomial values (16.6, 5.7 and 1.7 percent behind at p = 0.6 after 10, 50
and 100; 48.3 percent; 0.00025 percent) are pinned by tests to the digit.

## The backtest and the experiment log

The Validity tab's third block is the **walk-forward backtest**: every 63 sessions over about three years of stored
closes, the universe is scored with the closes known on that date only, using the price factors (momentum, trend, low
risk). Value and quality are left out because free fundamentals are not point-in-time (today's figures would leak into
the past), and news is left out because Alpaca's history is press releases only and would take hours to score; both
reasons are printed with the result. Each replayed label is scored 63 sessions later against SPY exactly as a live signal
is, and every replayed label goes through the same binomial, Monte Carlo and BEF tests, so there is a verdict on the
method months before the live log reaches 30. The replay uses today's S&P 100 list for the whole period, which is
survivorship bias, and says so. It reruns once a day or whenever a weight or threshold changes.

The **experiment log** counts every distinct set of weights and thresholds that has been backtested as one trial and
stores each trial's per-period Sharpe ratio, skew and kurtosis, because the **Deflated Sharpe Ratio** (Bailey and
Lopez de Prado) needs them: with N trials, the best Sharpe you would expect from luck alone is
SR0 = sqrt(V[SR]) x ((1 - g) Z(1 - 1/N) + g Z(1 - 1/(N e))) with g = 0.5772, and the DSR is the probability that the
current Sharpe is really above SR0, Phi((SR - SR0) sqrt(T - 1) / sqrt(1 - skew SR + (kurtosis - 1)/4 SR^2)). It
needs two trials to say anything. The point is the one the spec makes: tuning weights to the backtest is the trap, and
the counter makes the cost of every try visible.

## Sizing, correlation, dates, brief

The **Size** tab is the book's rule (page 65): sizing is only the choice of how much capital to put in and must never
change the signal, so it is a separate panel that reads nothing back into the scores. Type an account size and press
Enter: for each ticker, the shares such that a stop 2 x ATR(14) away loses 1 percent of the account, and the number of
option contracts whose total premium stays within 1 percent, using the ask of the contract the options check chose. The
ATR is the simple mean of the true range over 14 sessions, from the stored highs and lows. The same tab shows the
**correlation table** of the watchlist's daily returns over 90 sessions and warns when two holdings sit above 0.8.

The third line of the top strip is the **dates strip**: the next FOMC meeting and CPI release from `calendar.yaml` and
each ticker's next earnings date from Finnhub. Finnhub's economic calendar is a paid tier, so the Fed and CPI dates are
kept by hand in that file, each with the page it was read from and the day it was read; nothing in it was typed from
memory, and the 2027 FOMC dates carry the Fed's own "tentative" note.

`b` (or `--brief`) writes today's **brief** to `briefs/YYYY-MM-DD.md`: the market strip, the dates, and for each ticker
the price, the score, the options check and the newest DIRECT headlines, then the validity reports and the backtest.

## How the code is laid out

```
src/stock_terminal/
  __main__.py         entry point: the screen, or --check / --news / --peers / --reactions
  app.py              the Textual screen, keys, rendering
  provider.py         refresh() for prices (Board) and refresh_news() for the feeds (NewsBoard)
  relationships.py    relationships.yaml: per-ticker map and the keyword tables
  clients/base.py     the shared fetch routine: cache, rate limit, stale or missing on failure
  clients/alpaca_client.py   stock snapshots, the market clock, minute bars, daily closes, option contracts and chain snapshots
  clients/alpaca_news.py     Alpaca news
  clients/finnhub_client.py  Finnhub company news, general news, peers, basic financials, next earnings date
  clients/edgar_client.py    EDGAR filing lists, 8-K items, Form 4 XML parsing
  clients/fred_client.py     VIX and the 10-year yield
  news/models.py      NewsItem, the relation tag per ticker, Reaction, the feed shapes
  news/engine.py      one refresh: pull, tag, store, score, schedule and fill reactions, build feeds
  news/store.py       SQLite tables news and reactions (same file as the cache)
  news/dedupe.py      the same-story rule
  news/sentiment.py   FinBERT behind a swappable Scorer interface
  news/reactions.py   the rules for filling a reaction from bars
  quant/factors.py    the six factors as pure functions, inputs kept for the explain view
  quant/scoring.py    z-scores, composite, labels, the Score tab and explain text
  quant/engine.py     the score refresh: universe closes, fundamentals, news factor, signal log
  quant/store.py      SQLite tables daily_bars (with highs and lows), scores, signals, iv_history, backtests, experiments, settings
  quant/blackscholes.py  Black-Scholes prices, Greeks, implied vol, probabilities, the end-price simulation
  quant/options.py    the four-step options check and its text
  quant/validity.py   binomial sums, the Binomial Evolution Function, Monte Carlo, status, the report and its text
  quant/signal_scorer.py  scoring logged signals against stored closes (beat SPY, or beyond breakeven)
  quant/backtest.py   the walk-forward replay of the price factors and its report
  quant/experiments.py  the experiment log and the Deflated Sharpe Ratio
  quant/sizing.py     ATR, shares and contracts for the account, the correlation table
  quant/calendar.py   calendar.yaml and the dates strip
  quant/brief.py      the markdown brief
  datapoint.py        DataPoint: a value plus its source, age and status
  cache.py            SQLite cache (data/terminal.db)
  ratelimit.py        rolling-window rate limiter, one per client
  config.py           config.yaml with defaults and validation; Keys from .env (repr hides values)
  watchlist.py        watchlist.yaml, add / remove / save
  logging_setup.py    rotating log file (logs/terminal.log) and the hook for the log panel
tests/                pytest; no network, fakes for every client and the sentiment model
docs/SPEC.md          the spec
relationships.yaml    the relationship map and keyword tables (edit freely)
universe.yaml         the reference universe for the score (edit freely)
calendar.yaml         Fed and CPI dates with their sources (edit when the pages change)
briefs/               the daily briefs (gitignored)
```

Rules the code keeps: no order code anywhere; every outside call goes through a client with a rate limiter and the
cache so one failing source never crashes the app; a missing or stale value is labelled, never invented; keys are
read once from the environment and never shown.

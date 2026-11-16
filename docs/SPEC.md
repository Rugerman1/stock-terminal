# Stock Terminal: the spec

Tyler's build spec, written 2026-11-15. The master copy lives in his vault; this file is kept identical to it. A change to the spec happens there first, with Tyler's say-so, and is then synced here. Reference book: Berdondini, *The Theory of Quantitative Trading*, pages 36-42 (verification method), 51-58 (Binomial Evolution Function) and 65 (risk management).

## What I want built

Build me a terminal app (runs in my Windows terminal, keyboard driven) that does three jobs for a watchlist of stocks:

1. **News.** Shows news about each stock, plus news that could move it even when the stock is not named.
2. **Quant read.** Scores each stock for a long-term buy, and separately checks whether a call or a put makes sense right now, using stated formulas whose inputs I can see.
3. **Honesty check.** Tracks every signal it gives and tells me whether those signals are doing better than random chance.

Job 3 matters as much as jobs 1 and 2. My reference book (Berdondini, *The Theory of Quantitative Trading*) argues that a result only counts if it would be very unlikely to happen by luck. Read pages 36-42 (verification method), 51-58 (Binomial Evolution Function) and 65 (risk management) before designing anything, and build the app around that idea.

## Ground rules

- **Read-only.** This version places no orders. No trading code at all.
- **No promises.** Never label anything "buy" or "sell". Use Favorable / Neutral / Unfavorable and "Call candidate" / "Put candidate" / "No trade", and always show the signal's validity status next to it.
- **Show the math.** Every score has an explain view (press `?`) listing the formula, each input value and where the data came from.
- **Never invent data.** If a number is missing, delayed or stale, show it as missing, delayed or stale. No placeholder values.
- **I'm a beginner-to-intermediate coder.** Explain each step in plain words, define jargon the first time, show the exact command to run. When something breaks, tell me what broke and why before you fix it.

## Stack

- Python 3.11+, in a virtual environment. Give me PowerShell commands.
- Repo at `C:\Users\tyler\Claude Projects\stock-terminal` (local, not in OneDrive), git initialised, `.env` in `.gitignore`.
- **Textual** for the terminal UI.
- **SQLite** for caching, the signal log and stored volatility history.
- API keys in `.env`. Give me a `.env.example` and tell me where to get each key.

Data sources. Check each one's current docs for limits and what the free plan includes before writing code against it, and tell me if something I asked for needs a paid plan:

| Need | Source |
|---|---|
| Prices, live news stream, option chains | Alpaca (`alpaca-py`). I already use Alpaca. Free plan: IEX prices, "indicative" options feed (delayed, approximate quotes) |
| Company news, peers, earnings calendar, basic fundamentals | Finnhub free key |
| Filings (8-K) and insider trades (Form 4) | SEC EDGAR API (free, needs a User-Agent header with a contact email) |
| VIX close, 10-year Treasury yield | FRED API free key |
| Headline sentiment | FinBERT (`ProsusAI/finbert`) run locally, as a swappable module |

Wrap every source in its own client with a rate limiter and a cache, so one failing source never crashes the app.

## Screen layout

- **Top strip:** SPY, QQQ, VIX, 10-year yield, and a data-delay notice.
- **Left: watchlist table.** Ticker, price, % change, long-term label, options label, days to earnings, validity status.
- **Middle: news feed** for the selected ticker, newest first.
- **Right: detail panel** with tabs: Score, Options, Validity.
- Keys: `a` add ticker, `d` delete, `r` refresh, `?` explain, `q` quit. Watchlist saved in `watchlist.yaml`.

## Job 1: news engine

For each ticker build a "relationship map": its sector ETF, its 5 closest peers, and a short list of macro topics that matter to its sector (rates, oil, chips export rules and so on). Keep the map in a YAML file I can edit.

Pull news for the ticker and for everything in its map, then tag each headline:

- **DIRECT**: names the company
- **PEER**: names a peer or competitor
- **SECTOR**: about the sector or its ETF
- **MACRO**: Fed, inflation, jobs, rates, geopolitics
- **FILING**: an 8-K or insider trade from EDGAR

Also tag the catalyst type (earnings, guidance, analyst rating, M&A, legal/regulatory, product, insider, macro), run FinBERT for a sentiment score = P(positive) - P(negative), and remove duplicates of the same story from different outlets.

**Reaction tracker:** when a headline arrives, store the stock's price then, and again 1 hour, 1 day and 5 days later. Over time this becomes my own table of how much each kind of news really moves each stock.

## Job 2a: long-term score

Compare the stock with a reference universe (default: S&P 100, list in a config file, refreshed nightly). For each factor compute a z-score (how many standard deviations from the universe average), capped at -3 and +3.

| Factor | Definition | Weight |
|---|---|---|
| Momentum | price 21 trading days ago / price 252 trading days ago - 1 | 0.30 |
| Trend | price / 200-day average - 1 | 0.10 |
| Value | average of earnings yield and free-cash-flow yield | 0.20 |
| Quality | average of return on equity, gross margin, and (negative) debt-to-equity | 0.25 |
| Low risk | negative of 1-year realized volatility | 0.10 |
| News | time-weighted DIRECT sentiment, weight per headline = 0.5 ^ (age in days / 3), needs 5+ headlines | 0.05 |

Composite = weighted sum of the z-scores. Top 20% of the universe = Favorable, bottom 20% = Unfavorable, the rest Neutral.

These weights are starting guesses, fixed in a config file. Do not tune them to fit past data. Tuning on history is exactly the "creating knowledge randomly" trap the book warns about.

## Job 2b: call / put check

Step 1, direction. Short-term direction score = average of the z-scores of: 63-day return, price vs 50-day average, and 7-day DIRECT news sentiment. Only go further if it is beyond +1 (call side) or -1 (put side).

Step 2, pick a contract. 30-60 days to expiry, delta near 0.50. Skip contracts with open interest under 500 or a bid-ask spread wider than 10% of the mid price.

Step 3, compute and show all of these:

- **Realized volatility** (HV20, HV60) = standard deviation of daily log returns x sqrt(252)
- **Implied volatility (IV)** from the option snapshot. If the feed returns none, solve Black-Scholes backwards from the mid price. Compute Greeks yourself with Black-Scholes when the feed leaves them out.
- **Vol ratio** = IV / HV60. Above 1.3 = options expensive, below 1.0 = cheap.
- **IV rank** = (IV now - 1-year low) / (1-year high - 1-year low). Store one at-the-money IV per ticker per day in SQLite to build this history. Until there are 252 days, show "collecting, n/252".
- **Expected move** = price x IV x sqrt(days to expiry / 365)
- **Breakeven** = strike + ask premium (call) or strike - ask premium (put), and the % move needed to reach it
- **Probability of profit**, two versions side by side: the market's (lognormal using IV) and the model's (lognormal using HV60, zero drift)
- **Expected value** per contract = average payoff over 20,000 simulated end prices (HV60, zero drift) minus the ask premium, x 100
- **Max loss** = premium x 100

Step 4, label. "Call candidate" or "Put candidate" only if: direction passes, vol ratio is 1.2 or less, the needed move is no bigger than the expected move, and liquidity passes. Otherwise "No trade", with the failed check named. If earnings fall before expiry, show a warning that IV usually drops right after earnings.

## Job 3: validity engine (from the book)

Log every label the app produces: ticker, direction, time, price, horizon (63 trading days for long-term, the option's expiry for options). When the horizon passes, score it 1 or 0. For long-term signals, "1" means the stock beat SPY in the predicted direction, so a rising market does not count as skill.

Then run three tests on the last 50 scored signals:

1. **Binomial test.** Probability of k or more wins in n tries by luck, P(X >= k). Baseline win rate is 50% for beat-SPY outcomes; for raw up/down outcomes use how often random entries won over the same period (book p. 39-41).
2. **Monte Carlo.** 10,000 runs of random signals with the same count and holding periods. Report the share that did as well or better.
3. **Binomial Evolution Function**, the 4 steps on pages 51-55: force buy/sell alternation by inserting an opposite test trade in each gap, convert to 1/0, keep only trades that sit in a same-result consecutive pair, then binomial test at 50%.

Status shown everywhere a label appears:

- **UNPROVEN**: fewer than 30 scored signals
- **RANDOM-LIKE**: probability above 5%. Grey the labels out and show a banner. This is the book's "virtual mode": keep recording, stop trusting.
- **WEAK EVIDENCE**: between 1% and 5%
- **SIGNIFICANT**: below 1%

So I'm not waiting months, also build a walk-forward backtest that replays the scoring on past data and feeds the same three tests. Price-based factors and news (Alpaca history) can be replayed. Free fundamentals are not point-in-time, so leave Value and Quality out of the backtest and say so on screen rather than leak future data.

Keep an experiment log: every change to a weight or threshold adds one to a trial counter, and the Validity tab reports the Deflated Sharpe Ratio (Bailey and Lopez de Prado) using that count.

## Position sizing panel (book p. 65)

The book's rule: risk management is only the choice of how much capital to put in, and it must never change the signal. So sizing is a separate panel. I enter account size; it shows shares such that a stop at 2 x ATR loses 1% of the account, and for options the number of contracts whose total premium is 1% of the account or less.

## Extras

- Correlation table for the watchlist (90-day daily returns) with a warning when two holdings are above 0.8
- Earnings and Fed/CPI dates strip
- `brief` command that writes today's watchlist summary to a markdown file
- Log file for errors, shown in a collapsible panel

## Build order

Build one phase, show me how to run it, wait for my OK, then start the next.

1. Skeleton app, watchlist, prices, config and keys
2. News engine with tags, sentiment, dedupe
3. Long-term score with explain view
4. Options check
5. Signal log, three validity tests, status banner
6. Backtest, experiment log, sizing panel, extras

## Tests that must pass

Write unit tests with `pytest`. These values come from the book or from standard formulas:

- Binomial, win chance 60%: probability of being behind after 10 / 50 / 100 tosses = 16.6% / 5.7% / 1.7%
- Binomial, p = 0.60, n = 500, more than 300 wins: 48.3%
- Binomial, p = 0.40, n = 500, more than 250 wins: 0.00025%
- BEF on the sequence [1, 0, 1, 0, 1, 1, 0]: 2 independent trades, 2 wins, probability 25%
- Black-Scholes, S = 100, K = 100, 1 year, rate 5%, vol 20%: call 10.4506, put 5.5735, call delta 0.6368
- Expected move, price 100, IV 30%, 30 days: 8.60

Start by reading the PDF pages listed above, then give me a short plan and the list of API keys I need before writing any code.

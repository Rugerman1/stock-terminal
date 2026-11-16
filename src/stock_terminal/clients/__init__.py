"""One client per data source. Each owns a rate limiter and shares the SQLite cache,
so a failing source degrades to stale or missing values instead of crashing the app."""

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

```bash
# Local development (auto-bootstraps .venv on first run)
python app.py

# Or explicitly with uvicorn
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

There are no tests or linters configured. The app reads `.env` on startup via `python-dotenv`.

## Deployment

```bash
fly deploy                          # build Docker image and deploy
fly secrets set KEY=value           # set env vars on Fly.io (never committed)
fly volumes create trading212_data -r ams -n 1   # create persistent volume (once)
fly machines list -a trading212     # inspect running machines
fly volumes list -a trading212      # inspect volumes
```

State is uploaded to the live instance via:
```bash
curl -X POST https://trading212.fly.dev/api/state/import \
  -H "Content-Type: application/json" -d @state.json
```

## Architecture

Five modules with clear boundaries:

```
app.py          FastAPI app, WebSocket broadcast loop, all HTTP endpoints
bot.py          Strategy engine — owns positions, trades, logs, state file I/O
price_feed.py   3-layer price source: Twelve Data → Finnhub → yfinance (fallback)
t212_client.py  Trading212 REST wrapper (aiohttp, Basic auth, rate-limit retry)
templates/      Single Jinja2 template (index.html) — vanilla JS, Bootstrap 5.3 dark
```

**Data flow per tick (every 15 min):**
`bot._tick()` → `_process_stock()` per enabled ticker → `PriceFeed.get_quote()` (returns `{price, open}` in one API call) → buy/sell logic → `t212_client.place_market_order()` → `save_state()`

**State persistence:**
`bot.save_state()` writes to `STATE_FILE` (defaults to `state.json` next to the script; on Fly.io `DATA_DIR=/data` redirects it to the mounted volume). `bot.load_state()` is called once at startup inside the FastAPI `lifespan`. Default stocks (ASTI, POET) are added only when the state file has no stocks.

**WebSocket:**
`_broadcast_loop()` pushes `bot.get_state()` JSON to all connected clients every 5 seconds. `get_state()` is display-only (includes computed fields like `gain_pct`); `save_state()` uses a separate minimal format.

**Bilingual logs:**
`LogEntry` carries both `tr` (Turkish) and `en` (English) fields. The frontend picks the right field based on `currentLang` and stores both as `data-tr`/`data-en` attributes so language switching re-renders existing log lines without refetching.

**Trading212 auth:**
Basic auth with `base64(API_KEY:API_SECRET)`. Demo endpoint: `demo.trading212.com`, live: `live.trading212.com`. Controlled by `T212_DEMO` env var.

**Price feed efficiency:**
Twelve Data `/quote` returns both current price and day open in a single call (halves credit usage vs. separate endpoints). Finnhub and yfinance are fallbacks only. `CallStats` tracks per-provider usage, exposed at `GET /api/state` under `api_stats`.

## Key Env Vars

| Variable | Purpose |
|---|---|
| `T212_API_KEY` | Trading212 API key |
| `T212_API_SECRET` | Trading212 API secret (paired with key for Basic auth) |
| `T212_DEMO` | `true` = demo account, `false` = live |
| `TWELVEDATA_API_KEY` | Primary price source (800 calls/day free) |
| `FINNHUB_API_KEY` | Fallback price source (60 calls/min free) |
| `DATA_DIR` | Directory for `state.json` (default: script directory) |
| `PORT` | HTTP port (default: 8000; Fly.io uses 8080 via Dockerfile) |

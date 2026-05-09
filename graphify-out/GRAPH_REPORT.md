# Graph Report - .  (2026-05-09)

## Corpus Check
- Corpus is ~11,278 words - fits in a single context window. You may not need a graph.

## Summary
- 179 nodes · 281 edges · 17 communities (10 shown, 7 thin omitted)
- Extraction: 85% EXTRACTED · 15% INFERRED · 0% AMBIGUOUS · INFERRED: 43 edges (avg confidence: 0.56)
- Token cost: 12,000 input · 3,500 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Price Feed & Market Data|Price Feed & Market Data]]
- [[_COMMUNITY_FastAPI Routes & Auth|FastAPI Routes & Auth]]
- [[_COMMUNITY_Trading Strategy Flow|Trading Strategy Flow]]
- [[_COMMUNITY_Bot & State Management|Bot & State Management]]
- [[_COMMUNITY_Trading212 API Client|Trading212 API Client]]
- [[_COMMUNITY_State Sync & WebSocket|State Sync & WebSocket]]
- [[_COMMUNITY_Pydantic Models & Config|Pydantic Models & Config]]
- [[_COMMUNITY_Data Models & Bilingual UI|Data Models & Bilingual UI]]
- [[_COMMUNITY_Candle OHLC Data|Candle OHLC Data]]
- [[_COMMUNITY_App Core & Middleware|App Core & Middleware]]
- [[_COMMUNITY_WebSocket & Frontend|WebSocket & Frontend]]
- [[_COMMUNITY_Documentation|Documentation]]
- [[_COMMUNITY_PriceFeed Init|PriceFeed Init]]
- [[_COMMUNITY_State Export|State Export]]
- [[_COMMUNITY_T212 Client Core|T212 Client Core]]
- [[_COMMUNITY_Trading212 Error|Trading212 Error]]

## God Nodes (most connected - your core abstractions)
1. `Bot` - 27 edges
2. `Trading212Client` - 26 edges
3. `PriceFeed` - 17 edges
4. `StockConfig` - 12 edges
5. `Trading212Error` - 12 edges
6. `AuthMiddleware` - 8 edges
7. `Trade` - 7 edges
8. `StockRequest` - 7 edges
9. `AuthConnectRequest` - 7 edges
10. `AuthVerifyRequest` - 7 edges

## Surprising Connections (you probably didn't know these)
- `Bilingual UI (TR/EN Language Switch)` --semantically_similar_to--> `Bilingual Log Design (tr/en)`  [INFERRED] [semantically similar]
  templates/index.html → bot.py
- `StockRequest` --semantically_similar_to--> `StockConfig`  [INFERRED] [semantically similar]
  app.py → bot.py
- `State Persistence via state.json` --rationale_for--> `Bot.load_state`  [EXTRACTED]
  CLAUDE.md → bot.py
- `CLAUDE.md Project Instructions` --semantically_similar_to--> `Project Memory (project_tradingbot.md)`  [INFERRED] [semantically similar]
  CLAUDE.md → memory/project_tradingbot.md
- `StockConfig` --uses--> `PriceFeed`  [INFERRED]
  bot.py → price_feed.py

## Hyperedges (group relationships)
- **Per-Tick Execution Pipeline** — bot_tick, bot_processstock, pricefeed_getquote, bot_buy, t212client_placemarketorder, bot_savestate [EXTRACTED 1.00]
- **Three-Layer Price Provider Fallback** — pricefeed_tdquote, pricefeed_fhquote, pricefeed_yfquote [EXTRACTED 1.00]
- **Auth and Security Design** — app_authmiddleware, app_authconnect, app_t212keyram, index_authoverlay [EXTRACTED 0.95]

## Communities (17 total, 7 thin omitted)

### Community 0 - "Price Feed & Market Data"
Cohesion: 0.09
Nodes (16): CallStats, _fh_news(), _fh_quote(), get_stats(), PriceFeed, Fiyat kaynağı — üç katmanlı fallback zinciri:   1. Twelve Data  /quote  (1 çağrı, Fallback zinciri: TD → FH → YF, Tek çağrıda hem price hem open — bot bunu kullanır. (+8 more)

### Community 1 - "FastAPI Routes & Auth"
Cohesion: 0.09
Nodes (14): BaseHTTPMiddleware, auth_connect(), auth_disconnect(), auth_verify(), AuthMiddleware, _broadcast_loop(), get_state(), _hash_pw() (+6 more)

### Community 2 - "Trading Strategy Flow"
Cohesion: 0.08
Nodes (29): auth_connect endpoint, _hash_pw, GET /api/t212/search endpoint, T212 Keys in RAM Only Design, Bot._buy, Bot._check_exit, Bot._process_stock, Bot._refresh_reference (+21 more)

### Community 3 - "Bot & State Management"
Cohesion: 0.18
Nodes (4): Bot, LogEntry, Position, Trade

### Community 4 - "Trading212 API Client"
Cohesion: 0.19
Nodes (3): Return the T212 ticker string for a given symbol, or None if not found., quantity > 0 = buy, quantity < 0 = sell, Trading212Client

### Community 5 - "State Sync & WebSocket"
Cohesion: 0.18
Nodes (14): _broadcast_loop, GET /api/state endpoint, POST /api/state/import endpoint, lifespan, Bot.get_state, Bot.load_state, CallStats, 3-Layer Price Fallback Chain (+6 more)

### Community 6 - "Pydantic Models & Config"
Cohesion: 0.26
Nodes (10): BaseModel, Exception, add_stock(), AuthConnectRequest, AuthVerifyRequest, lifespan(), _load_config(), StockRequest (+2 more)

### Community 7 - "Data Models & Bilingual UI"
Cohesion: 0.25
Nodes (8): StockRequest, Bilingual Log Design (tr/en), Bot, LogEntry, Position, StockConfig, Trade, Bilingual UI (TR/EN Language Switch)

### Community 8 - "Candle OHLC Data"
Cohesion: 0.67
Nodes (3): PriceFeed.get_candles_15m, _td_candles, _yf_candles_sync

## Knowledge Gaps
- **40 isolated node(s):** `Fiyat kaynağı — üç katmanlı fallback zinciri:   1. Twelve Data  /quote  (1 çağrı`, `Tek çağrıda hem anlık fiyat hem gün açılışı.`, `Fallback zinciri: TD → FH → YF`, `Tek çağrıda hem price hem open — bot bunu kullanır.`, `Son `days` günde kapanış fiyatı % değişimi.` (+35 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `PriceFeed` connect `Price Feed & Market Data` to `FastAPI Routes & Auth`, `Bot & State Management`, `Pydantic Models & Config`?**
  _High betweenness centrality (0.166) - this node is a cross-community bridge._
- **Why does `Trading212Client` connect `Trading212 API Client` to `FastAPI Routes & Auth`, `Bot & State Management`, `Pydantic Models & Config`?**
  _High betweenness centrality (0.119) - this node is a cross-community bridge._
- **Why does `Bot` connect `Bot & State Management` to `Price Feed & Market Data`, `FastAPI Routes & Auth`, `Trading212 API Client`, `Pydantic Models & Config`?**
  _High betweenness centrality (0.115) - this node is a cross-community bridge._
- **Are the 8 inferred relationships involving `Bot` (e.g. with `PriceFeed` and `Trading212Client`) actually correct?**
  _`Bot` has 8 INFERRED edges - model-reasoned connections that need verification._
- **Are the 10 inferred relationships involving `Trading212Client` (e.g. with `StockConfig` and `Position`) actually correct?**
  _`Trading212Client` has 10 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `PriceFeed` (e.g. with `StockConfig` and `Position`) actually correct?**
  _`PriceFeed` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `StockConfig` (e.g. with `PriceFeed` and `Trading212Client`) actually correct?**
  _`StockConfig` has 9 INFERRED edges - model-reasoned connections that need verification._
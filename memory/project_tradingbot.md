---
name: trading212-bot project
description: FastAPI tabanlı otomatik trading botu — Trading212 entegrasyonu, 5 modül, web UI, Fly.io deploy
type: project
---

FastAPI + vanilla JS tabanlı otomatik trading botu. Trading212 API üzerinden ASTI, POET gibi ABD hisselerinde alım/satım yapıyor.

**Why:** Gün açılışından belirli % düşen hisseleri otomatik alıp, stop-loss/take-profit ile yöneterek pasif strateji yürütmek.

**How to apply:** Yeni özellik önerirken: mevcut 5-modül sınırını koru, state.json basitliğini boz, T212 key'lerini diske yazma.

## Mimari (5 modül)
- `app.py` — FastAPI, WebSocket broadcast, tüm HTTP endpoint'ler, auth middleware
- `bot.py` — Strateji motoru: pozisyonlar, işlemler, loglar, state I/O
- `price_feed.py` — 3 katmanlı fallback: Twelve Data → Finnhub → yfinance
- `t212_client.py` — Trading212 REST wrapper (aiohttp, Basic auth, rate-limit retry)
- `templates/index.html` — Tek Jinja2 şablonu, vanilla JS, Bootstrap 5.3 dark

## Strateji
- 15 dakikada bir tick
- Gün açılışından `buy_drop_pct` % düşünce alım sinyali
- Stop-loss ve take-profit yüzdesi pozisyona özgü
- `max_investment` / price = adet (tam sayı, yuvarlanır)

## Güvenlik tasarımı
- T212 API key'leri sadece RAM'de tutulur, diske asla yazılmaz
- UI şifresinin sadece PBKDF2 hash'i `config.json`'a yazılır
- Sunucu yeniden başlarsa T212 key yeniden girilmeli (browser localStorage'dan otomatik dener)
- Auth middleware: `/`, `/ws`, `/api/auth/*` muaf; diğerleri X-UI-Password header kontrolü

## State
- `state.json` (DATA_DIR ortam değişkeni ile override — Fly.io'da `/data`)
- Kaydedilenler: stocks config, positions, trades, reference_prices (sadece bugün için)
- T212 key'leri ve UI şifresi state.json'a yazılmaz

## Deploy
- Fly.io: `fly deploy`, `fly secrets set KEY=value`
- Persistent volume: trading212_data, /data mount
- State import: `curl -X POST .../api/state/import -d @state.json`
- App: trading212.fly.dev

## Fiyat kaynakları
- Twelve Data: `/quote` tek çağrıda price + open (800 çağrı/gün free)
- Finnhub: fallback (60 çağrı/dk free)
- yfinance: son çare fallback (thread executor)
- CallStats ile kullanım takibi, GET /api/state'te api_stats alanında

## UI özellikleri
- Bilingual (TR/EN), localStorage'da dil tercihi
- WebSocket ile 5 sn'de bir realtime update
- Hisse kartları: fiyat, düşüş bar'ı, pozisyon P&L, manual sell
- Auth overlay: login → T212 connect akışı
- DEMO/LIVE badge, hesap bakiyesi (30sn'de bir)

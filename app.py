import os
import sys

# Otomatik venv — hangi python ile çalıştırılırsa çalıştırılsın
_venv_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
if os.path.exists(_venv_py) and os.path.abspath(sys.executable) != os.path.abspath(_venv_py):
    os.execv(_venv_py, [_venv_py] + sys.argv)

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional, Set

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from bot import Bot, StockConfig, STATE_FILE
import price_feed as _pf
from price_feed import PriceFeed
from t212_client import Trading212Client

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Fiyat kaynakları başlatma
_td_key = os.getenv("TWELVEDATA_API_KEY", "")
_fh_key = os.getenv("FINNHUB_API_KEY", "")
_pf.init(twelvedata_key=_td_key, finnhub_key=_fh_key)
if not _td_key and not _fh_key:
    logging.warning("Hiçbir fiyat API key'i ayarlı değil")

t212: Optional[Trading212Client] = None
feed: PriceFeed = PriceFeed()
bot: Optional[Bot] = None
_ws_clients: Set[WebSocket] = set()
templates = Jinja2Templates(directory="templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global t212, bot
    api_key = os.getenv("T212_API_KEY", "")
    api_secret = os.getenv("T212_API_SECRET", "")
    demo = os.getenv("T212_DEMO", "true").lower() != "false"

    t212 = Trading212Client(api_key=api_key, api_secret=api_secret, demo=demo)
    bot = Bot(t212, feed)

    bot.load_state()

    # İlk çalıştırmada state dosyası yoksa varsayılan hisseleri ekle
    if not bot.stocks:
        bot.add_stock(StockConfig(ticker="ASTI"))
        bot.add_stock(StockConfig(ticker="POET"))

    asyncio.create_task(_broadcast_loop())
    yield

    await bot.stop()
    await t212.close()


app = FastAPI(title="Trading212 Bot", lifespan=lifespan)


# ─── Pydantic request models ────────────────────────────────────────────────

class StockRequest(BaseModel):
    ticker: str
    t212_ticker: str = ""
    buy_drop_pct: float = 10.0
    stop_loss_pct: float = 10.0
    take_profit_pct: float = 10.0
    max_investment: float = 1000.0
    enabled: bool = True


# ─── HTML ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ─── Bot control ─────────────────────────────────────────────────────────────

@app.post("/api/bot/start")
async def start_bot():
    await bot.start()
    return {"ok": True, "running": bot.running}


@app.post("/api/bot/stop")
async def stop_bot():
    await bot.stop()
    return {"ok": True, "running": bot.running}


@app.post("/api/bot/tick")
async def manual_tick():
    """Stratejiyi hemen tetikle (test için)"""
    await bot.manual_tick()
    return {"ok": True}


@app.post("/api/stocks/{ticker}/sell")
async def manual_sell(ticker: str):
    """Açık pozisyonu anlık fiyattan manuel sat"""
    if ticker not in bot.positions:
        return {"error": f"{ticker} için açık pozisyon yok"}
    pos = bot.positions[ticker]
    price = await feed.get_price(ticker)
    if price is None:
        return {"error": "Anlık fiyat alınamadı"}
    await bot._sell(ticker, pos, "manual", price)
    return {"ok": True}


# ─── State ───────────────────────────────────────────────────────────────────

@app.get("/api/state")
async def get_state():
    state = bot.get_state()
    state["api_stats"] = _pf.get_stats()
    return state


@app.get("/api/account")
async def get_account():
    try:
        return await t212.get_account_summary()
    except Exception as e:
        return {"error": str(e)}


# ─── Stock management ────────────────────────────────────────────────────────

@app.post("/api/stocks")
async def add_stock(req: StockRequest):
    cfg = StockConfig(**req.model_dump())
    bot.add_stock(cfg)
    return {"ok": True, "ticker": cfg.ticker}


@app.put("/api/stocks/{ticker}")
async def update_stock(ticker: str, req: StockRequest):
    if ticker not in bot.stocks:
        return {"error": "hisse bulunamadı"}
    data = req.model_dump(exclude={"ticker"})
    bot.update_stock(ticker, **data)
    return {"ok": True}


@app.delete("/api/stocks/{ticker}")
async def remove_stock(ticker: str):
    bot.remove_stock(ticker)
    return {"ok": True}


# ─── State import / export ───────────────────────────────────────────────────

@app.get("/api/state/export")
async def export_state():
    """İndirilebilir ham state (hisse listesi + işlem geçmişi)"""
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/state/import")
async def import_state(request: Request):
    """Ham state JSON'ını yükle ve botu yeniden başlat"""
    data = await request.json()
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # Mevcut state'i temizle ve yeniden yükle
    bot.stocks.clear()
    bot.positions.clear()
    bot.trades.clear()
    bot.reference_prices.clear()
    bot.reference_dates.clear()
    bot.current_prices.clear()
    bot.load_state()
    return {"ok": True, "stocks": len(bot.stocks), "trades": len(bot.trades)}


# ─── Price data ──────────────────────────────────────────────────────────────

@app.get("/api/candles/{ticker}")
async def get_candles(ticker: str):
    return await feed.get_candles_15m(ticker)


# ─── WebSocket ───────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        _ws_clients.discard(ws)


async def _broadcast_loop():
    """Her 5 saniyede bir tüm bağlı istemcilere state gönder"""
    while True:
        if _ws_clients and bot:
            state = bot.get_state()
            msg = json.dumps(state)
            dead: Set[WebSocket] = set()
            for ws in list(_ws_clients):
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.add(ws)
            _ws_clients.difference_update(dead)
        await asyncio.sleep(5)


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)

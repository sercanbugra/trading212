import os
import sys

_venv_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
if os.path.exists(_venv_py) and os.path.abspath(sys.executable) != os.path.abspath(_venv_py):
    os.execv(_venv_py, [_venv_py] + sys.argv)

import asyncio
import hashlib
import json
import logging
import secrets as _secrets
from contextlib import asynccontextmanager
from typing import Optional, Set

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from bot import Bot, StockConfig, STATE_FILE
import price_feed as _pf
from price_feed import PriceFeed
from t212_client import Trading212Client, Trading212Error

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# ─── Price feed (T212 key'leri buraya gelmiyor) ───────────────────────────────
_td_key = os.getenv("TWELVEDATA_API_KEY", "")
_fh_key = os.getenv("FINNHUB_API_KEY", "")
_pf.init(twelvedata_key=_td_key, finnhub_key=_fh_key)

# ─── Config dosyası — sadece UI şifre hash'i, T212 key'leri ASLA diske yazılmaz
_data_dir = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_data_dir, "config.json")

# RAM'de tutulan kimlik bilgileri — yeniden başlatmada sıfırlanır
_t212_key: str = ""
_t212_secret: str = ""
_t212_demo: bool = True
_t212_connected: bool = False
_ui_pw_hash: str = ""
_ui_pw_enabled: bool = False
_instruments_cache: list = []
_instruments_ts: float = 0.0


def _hash_pw(password: str) -> str:
    salt = _secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}:{key.hex()}"


def _verify_pw(password: str, stored: str) -> bool:
    try:
        salt, key_hex = stored.split(":", 1)
        key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
        return _secrets.compare_digest(key.hex(), key_hex)
    except Exception:
        return False


def _load_config():
    global _ui_pw_hash, _ui_pw_enabled
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        _ui_pw_hash = cfg.get("ui_password_hash", "")
        _ui_pw_enabled = bool(_ui_pw_hash)


def _save_config():
    os.makedirs(_data_dir, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"ui_password_hash": _ui_pw_hash}, f)


def _ui_auth_ok(request: Request) -> bool:
    if not _ui_pw_enabled:
        return True
    pw = request.headers.get("X-UI-Password", "")
    return bool(pw) and _verify_pw(pw, _ui_pw_hash)


# ─── Auth Middleware ──────────────────────────────────────────────────────────

class AuthMiddleware(BaseHTTPMiddleware):
    _EXEMPT = {"/", "/ws", "/api/auth/status", "/api/auth/verify", "/api/auth/connect"}

    async def dispatch(self, request: Request, call_next):
        if request.url.path not in self._EXEMPT and _ui_pw_enabled:
            if not _ui_auth_ok(request):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


# ─── Global state ─────────────────────────────────────────────────────────────

feed = PriceFeed()
bot: Optional[Bot] = None
templates = Jinja2Templates(directory="templates")
_ws_clients: Set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot
    _load_config()
    bot = Bot(t212=None, feed=feed)
    bot.load_state()
    if not bot.stocks:
        bot.add_stock(StockConfig(ticker="ASTI"))
        bot.add_stock(StockConfig(ticker="POET"))
    asyncio.create_task(_broadcast_loop())
    yield
    await bot.stop()
    if bot.t212:
        await bot.t212.close()


app = FastAPI(title="Trading212 Bot", lifespan=lifespan)
app.add_middleware(AuthMiddleware)


# ─── Pydantic modelleri ───────────────────────────────────────────────────────

class StockRequest(BaseModel):
    ticker: str
    t212_ticker: str = ""
    buy_drop_pct: float = 10.0
    stop_loss_pct: float = 10.0
    take_profit_pct: float = 10.0
    max_investment: float = 1000.0
    enabled: bool = True
    xday_period: int = 0
    xday_buy_drop: float = 0.0
    xday_sell_rise: float = 0.0
    sentiment_sell_below: float = 0.0
    sentiment_buy_above: float = 0.0
    target_price: float = 0.0


class AuthConnectRequest(BaseModel):
    t212_key: str
    t212_secret: str
    demo: bool = True
    ui_password: str = ""


class AuthVerifyRequest(BaseModel):
    ui_password: str


# ─── Auth endpoint'leri ───────────────────────────────────────────────────────

@app.get("/api/auth/status")
async def auth_status():
    return {
        "ui_password_required": _ui_pw_enabled,
        "t212_connected": _t212_connected,
        "demo": _t212_demo,
    }


@app.post("/api/auth/verify")
async def auth_verify(req: AuthVerifyRequest):
    if not _ui_pw_enabled:
        return {"ok": True}
    if not _verify_pw(req.ui_password, _ui_pw_hash):
        raise HTTPException(400, "Şifre yanlış / Wrong password")
    return {"ok": True}


@app.post("/api/auth/connect")
async def auth_connect(req: AuthConnectRequest, request: Request):
    """T212 key'lerini RAM'e al ve bot'u bağla. İlk bağlantıda UI şifresini ayarla."""
    global _t212_key, _t212_secret, _t212_demo, _t212_connected
    global _ui_pw_hash, _ui_pw_enabled

    if _ui_pw_enabled and not _ui_auth_ok(request):
        raise HTTPException(401, "Unauthorized")

    # T212 key'lerini doğrula
    test = Trading212Client(api_key=req.t212_key, api_secret=req.t212_secret, demo=req.demo)
    try:
        await test.get_account_summary()
    except Trading212Error as e:
        raise HTTPException(400, f"T212 bağlantısı başarısız: {e}")
    finally:
        await test.close()

    # Sadece RAM'e yaz — diske asla
    _t212_key = req.t212_key
    _t212_secret = req.t212_secret
    _t212_demo = req.demo
    _t212_connected = True

    # UI şifresi henüz ayarlı değilse ve kullanıcı ayarlamak istiyorsa
    if req.ui_password and not _ui_pw_enabled:
        _ui_pw_hash = _hash_pw(req.ui_password)
        _ui_pw_enabled = True
        _save_config()

    # Bot'un T212 client'ını güncelle
    if bot.t212:
        await bot.t212.close()
    bot.t212 = Trading212Client(api_key=_t212_key, api_secret=_t212_secret, demo=_t212_demo)

    return {"ok": True, "demo": _t212_demo}


@app.post("/api/auth/disconnect")
async def auth_disconnect(request: Request):
    """T212 bağlantısını kes (key'leri RAM'den sil)."""
    global _t212_key, _t212_secret, _t212_connected
    if not _ui_auth_ok(request):
        raise HTTPException(401)
    await bot.stop()
    if bot.t212:
        await bot.t212.close()
        bot.t212 = None
    _t212_key = _t212_secret = ""
    _t212_connected = False
    return {"ok": True}


# ─── HTML ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ─── Bot kontrolü ─────────────────────────────────────────────────────────────

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
    await bot.manual_tick()
    return {"ok": True}


@app.post("/api/stocks/{ticker}/sell")
async def manual_sell(ticker: str):
    if ticker not in bot.positions:
        return {"error": f"{ticker} için açık pozisyon yok"}
    pos = bot.positions[ticker]
    price = await feed.get_price(ticker)
    if price is None:
        return {"error": "Anlık fiyat alınamadı"}
    await bot._sell(ticker, pos, "manual", price)
    return {"ok": True}


# ─── State ────────────────────────────────────────────────────────────────────

@app.get("/api/state")
async def get_state():
    state = bot.get_state()
    state["api_stats"] = _pf.get_stats()
    state["t212_connected"] = _t212_connected
    state["demo"] = _t212_demo
    return state


@app.get("/api/account")
async def get_account():
    if not _t212_connected or bot.t212 is None:
        return {"error": "T212 bağlı değil"}
    try:
        return await bot.t212.get_account_summary()
    except Exception as e:
        return {"error": str(e)}


# ─── Hisse yönetimi ───────────────────────────────────────────────────────────

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


# ─── T212 enstrüman arama ────────────────────────────────────────────────────

@app.get("/api/t212/search")
async def search_t212_instruments(q: str = ""):
    import time
    global _instruments_cache, _instruments_ts
    if not _t212_connected or bot.t212 is None:
        return {"error": "T212 bağlı değil"}
    if not _instruments_cache or (time.time() - _instruments_ts) > 21600:
        try:
            _instruments_cache = await bot.t212.get_instruments()
            _instruments_ts = time.time()
        except Exception as e:
            return {"error": str(e)}
    if not q:
        return []
    q_lower = q.lower()
    results = [
        {"ticker": inst["ticker"], "name": inst.get("shortName") or inst.get("name", "")}
        for inst in _instruments_cache
        if q_lower in inst["ticker"].lower() or q_lower in (inst.get("shortName") or "").lower()
    ][:15]
    return results


# ─── Fiyat verisi ─────────────────────────────────────────────────────────────

@app.get("/api/candles/{ticker}")
async def get_candles(ticker: str):
    return await feed.get_candles_15m(ticker)


# ─── State import / export ────────────────────────────────────────────────────

@app.get("/api/state/export")
async def export_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/state/import")
async def import_state(request: Request):
    data = await request.json()
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    bot.stocks.clear()
    bot.positions.clear()
    bot.trades.clear()
    bot.reference_prices.clear()
    bot.reference_dates.clear()
    bot.current_prices.clear()
    bot.load_state()
    return {"ok": True, "stocks": len(bot.stocks), "trades": len(bot.trades)}


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, p: str = ""):
    if _ui_pw_enabled and not _verify_pw(p, _ui_pw_hash):
        await ws.close(code=4001)
        return
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        _ws_clients.discard(ws)


async def _broadcast_loop():
    while True:
        if _ws_clients and bot:
            state = bot.get_state()
            state["t212_connected"] = _t212_connected
            state["demo"] = _t212_demo
            state["api_stats"] = _pf.get_stats()
            msg = json.dumps(state)
            dead: Set[WebSocket] = set()
            for ws in list(_ws_clients):
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.add(ws)
            _ws_clients.difference_update(dead)
        await asyncio.sleep(5)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)

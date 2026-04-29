"""
Fiyat kaynağı — üç katmanlı fallback zinciri:
  1. Twelve Data  /quote  (1 çağrı = fiyat + gün açılışı)
  2. Finnhub      /quote
  3. yfinance     (yerel, rate-limit riski var)

Env değişkenleri:
  TWELVEDATA_API_KEY
  FINNHUB_API_KEY      (opsiyonel)
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional

import aiohttp

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=4)

# ---------- Konfigürasyon (app.py tarafından set edilir) ----------
_td_key: str = ""
_fh_key: str = ""


def init(twelvedata_key: str = "", finnhub_key: str = ""):
    global _td_key, _fh_key
    _td_key = twelvedata_key
    _fh_key = finnhub_key


# ---------- Çağrı sayacı ----------
@dataclass
class CallStats:
    twelvedata: int = 0
    finnhub: int = 0
    yfinance: int = 0
    errors: int = 0

stats = CallStats()


def get_stats() -> dict:
    return {
        "twelvedata": stats.twelvedata,
        "finnhub": stats.finnhub,
        "yfinance": stats.yfinance,
        "errors": stats.errors,
        "total": stats.twelvedata + stats.finnhub + stats.yfinance,
    }


# ---------- Twelve Data ----------

async def _td_quote(ticker: str) -> Optional[dict]:
    """Tek çağrıda hem anlık fiyat hem gün açılışı."""
    if not _td_key:
        return None
    try:
        params = {"symbol": ticker, "apikey": _td_key}
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.twelvedata.com/quote", params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json()
                if data.get("status") == "error":
                    logger.warning(f"[TD/{ticker}] {data.get('message')}")
                    return None
                stats.twelvedata += 1
                return {
                    "price": float(data["close"]),
                    "open":  float(data["open"]),
                }
    except Exception as e:
        logger.warning(f"[TD/{ticker}] {e}")
    return None


async def _td_candles(ticker: str) -> List[dict]:
    if not _td_key:
        return []
    try:
        params = {
            "symbol": ticker,
            "interval": "15min",
            "outputsize": 30,
            "apikey": _td_key,
        }
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.twelvedata.com/time_series", params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                if data.get("status") == "error":
                    return []
                stats.twelvedata += 1
                values = data.get("values", [])
                return [
                    {
                        "time":   bar["datetime"],
                        "open":   round(float(bar["open"]),  4),
                        "high":   round(float(bar["high"]),  4),
                        "low":    round(float(bar["low"]),   4),
                        "close":  round(float(bar["close"]), 4),
                        "volume": int(bar.get("volume", 0)),
                    }
                    for bar in reversed(values)
                ]
    except Exception as e:
        logger.warning(f"[TD candles/{ticker}] {e}")
    return []


# ---------- Finnhub ----------

async def _fh_quote(ticker: str) -> Optional[dict]:
    if not _fh_key:
        return None
    try:
        params = {"symbol": ticker, "token": _fh_key}
        async with aiohttp.ClientSession() as s:
            async with s.get("https://finnhub.io/api/v1/quote", params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json()
                price = data.get("c")  # current
                open_ = data.get("o")  # day open
                if not price or price == 0:
                    return None
                stats.finnhub += 1
                return {
                    "price": float(price),
                    "open":  float(open_) if open_ else None,
                }
    except Exception as e:
        logger.warning(f"[FH/{ticker}] {e}")
    return None


# ---------- yfinance (sync → executor) ----------

def _yf_quote_sync(ticker: str) -> Optional[dict]:
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)

        price = None
        try:
            p = stock.fast_info.last_price
            if p and p > 0:
                price = float(p)
        except Exception:
            pass

        if price is None:
            data = stock.history(period="1d", interval="1m")
            if not data.empty:
                price = float(data["Close"].iloc[-1])

        if price is None:
            return None

        open_ = None
        try:
            day = stock.history(period="1d", interval="1d")
            if not day.empty:
                open_ = float(day["Open"].iloc[-1])
        except Exception:
            pass

        stats.yfinance += 1
        return {"price": price, "open": open_}
    except Exception as e:
        logger.warning(f"[YF/{ticker}] {e}")
    return None


async def _yf_quote(ticker: str) -> Optional[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _yf_quote_sync, ticker)


def _yf_candles_sync(ticker: str) -> List[dict]:
    try:
        import yfinance as yf
        data = yf.Ticker(ticker).history(period="1d", interval="15m")
        if data.empty:
            return []
        stats.yfinance += 1
        return [
            {
                "time":   ts.isoformat(),
                "open":   round(float(row["Open"]),  4),
                "high":   round(float(row["High"]),  4),
                "low":    round(float(row["Low"]),   4),
                "close":  round(float(row["Close"]), 4),
                "volume": int(row["Volume"]),
            }
            for ts, row in data.iterrows()
        ]
    except Exception as e:
        logger.warning(f"[YF candles/{ticker}] {e}")
    return []


# ---------- PriceFeed ----------

class PriceFeed:

    async def _quote(self, ticker: str) -> Optional[dict]:
        """Fallback zinciri: TD → FH → YF"""
        result = await _td_quote(ticker)
        if result:
            return result

        logger.info(f"[{ticker}] TD başarısız, Finnhub deneniyor")
        result = await _fh_quote(ticker)
        if result:
            return result

        logger.info(f"[{ticker}] Finnhub başarısız, yfinance deneniyor")
        result = await _yf_quote(ticker)
        if result:
            return result

        stats.errors += 1
        logger.error(f"[{ticker}] Tüm kaynaklar başarısız")
        return None

    async def get_price(self, ticker: str) -> Optional[float]:
        q = await self._quote(ticker)
        return q["price"] if q else None

    async def get_day_open(self, ticker: str) -> Optional[float]:
        q = await self._quote(ticker)
        return q.get("open") if q else None

    async def get_quote(self, ticker: str) -> Optional[dict]:
        """Tek çağrıda hem price hem open — bot bunu kullanır."""
        return await self._quote(ticker)

    async def get_candles_15m(self, ticker: str) -> List[dict]:
        result = await _td_candles(ticker)
        if result:
            return result
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, _yf_candles_sync, ticker)

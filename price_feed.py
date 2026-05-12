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
import time as _time
from datetime import date, datetime, timedelta
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


TD_DAILY_LIMIT = 800

# ---------- Çağrı sayacı ----------
@dataclass
class CallStats:
    twelvedata: int = 0
    finnhub: int = 0
    yfinance: int = 0
    errors: int = 0
    reset_date: str = field(default_factory=lambda: date.today().isoformat())

    def maybe_reset(self):
        today = date.today().isoformat()
        if self.reset_date != today:
            self.twelvedata = 0
            self.finnhub = 0
            self.yfinance = 0
            self.errors = 0
            self.reset_date = today

stats = CallStats()


def get_stats() -> dict:
    stats.maybe_reset()
    remaining = max(0, TD_DAILY_LIMIT - stats.twelvedata)
    return {
        "twelvedata": stats.twelvedata,
        "twelvedata_limit": TD_DAILY_LIMIT,
        "twelvedata_remaining": remaining,
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


# ─── Günlük mum (X-gün değişimi) ─────────────────────────────────────────────

async def _td_daily(ticker: str, days: int) -> List[dict]:
    if not _td_key:
        return []
    try:
        params = {"symbol": ticker, "interval": "1day", "outputsize": days, "apikey": _td_key}
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.twelvedata.com/time_series", params=params,
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                if data.get("status") == "error":
                    return []
                stats.twelvedata += 1
                return [{"close": float(bar["close"]), "date": bar["datetime"]}
                        for bar in reversed(data.get("values", []))]
    except Exception as e:
        logger.warning(f"[TD daily/{ticker}] {e}")
    return []


def _yf_daily_sync(ticker: str, days: int) -> List[dict]:
    try:
        import yfinance as yf
        data = yf.Ticker(ticker).history(period=f"{days + 5}d", interval="1d")
        if data.empty:
            return []
        rows = [{"close": float(row["Close"]), "date": str(ts.date())} for ts, row in data.iterrows()]
        return rows[-days:] if len(rows) >= days else rows
    except Exception as e:
        logger.warning(f"[YF daily/{ticker}] {e}")
    return []


# ─── Sentiment (VADER + haber) ────────────────────────────────────────────────

_sentiment_cache: dict = {}   # ticker -> (score, timestamp)
_SENTIMENT_TTL = 3600         # 1 saat


async def _fh_news(ticker: str) -> List[str]:
    if not _fh_key:
        return []
    try:
        now = datetime.now()
        cutoff = now - timedelta(days=2)
        cutoff_ts = cutoff.timestamp()
        params = {
            "symbol": ticker,
            "from": cutoff.strftime("%Y-%m-%d"),
            "to": now.strftime("%Y-%m-%d"),
            "token": _fh_key,
        }
        async with aiohttp.ClientSession() as s:
            async with s.get("https://finnhub.io/api/v1/company-news", params=params,
                             timeout=aiohttp.ClientTimeout(total=8)) as r:
                items = await r.json()
                return [(i.get("headline", "") + " " + i.get("summary", "")).strip()
                        for i in (items or [])[:20]
                        if i.get("datetime", 0) >= cutoff_ts]
    except Exception as e:
        logger.warning(f"[FH news/{ticker}] {e}")
    return []


def _yf_news_sync(ticker: str) -> List[str]:
    try:
        import yfinance as yf
        import time as _t
        cutoff_ts = _t.time() - 2 * 86400
        news = yf.Ticker(ticker).news or []
        result = []
        for n in news[:20]:
            if n.get("providerPublishTime", 0) < cutoff_ts:
                continue
            title = (n.get("content", {}).get("title", "") or n.get("title", "")).strip()
            if title:
                result.append(title)
        return result
    except Exception as e:
        logger.warning(f"[YF news/{ticker}] {e}")
    return []


def _vader_score(texts: List[str]) -> Optional[float]:
    filtered = [t for t in texts if t.strip()]
    if not filtered:
        return None
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        analyzer = SentimentIntensityAnalyzer()
        scores = [analyzer.polarity_scores(t)["compound"] for t in filtered]
        return sum(scores) / len(scores) * 100  # -100..100
    except ImportError:
        logger.warning("vaderSentiment yüklü değil — pip install vaderSentiment")
    except Exception as e:
        logger.warning(f"VADER hatası: {e}")
    return None


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

    async def get_day_change(self, ticker: str, days: int) -> Optional[float]:
        """Son `days` günde kapanış fiyatı % değişimi."""
        candles = await _td_daily(ticker, days + 1)
        if not candles:
            loop = asyncio.get_event_loop()
            candles = await loop.run_in_executor(_executor, _yf_daily_sync, ticker, days + 1)
        if len(candles) < 2:
            return None
        old_close = candles[0]["close"]
        new_close = candles[-1]["close"]
        if old_close == 0:
            return None
        return (new_close - old_close) / old_close * 100

    async def get_sentiment(self, ticker: str) -> Optional[float]:
        """VADER haber skoru (-100..100). 1 saat önbellek."""
        cached = _sentiment_cache.get(ticker)
        if cached and (_time.time() - cached[1]) < _SENTIMENT_TTL:
            return cached[0]
        texts = await _fh_news(ticker)
        if not texts:
            loop = asyncio.get_event_loop()
            texts = await loop.run_in_executor(_executor, _yf_news_sync, ticker)
        score = _vader_score(texts)
        _sentiment_cache[ticker] = (score, _time.time())
        return score

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

_data_dir = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(_data_dir, "state.json")

from price_feed import PriceFeed
from t212_client import Trading212Client, Trading212Error

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 15 * 60  # 15 dakika


@dataclass
class StockConfig:
    ticker: str
    t212_ticker: str = ""
    buy_drop_pct: float = 10.0
    stop_loss_pct: float = 10.0
    take_profit_pct: float = 10.0
    max_investment: float = 1000.0
    enabled: bool = True

    def __post_init__(self):
        if not self.t212_ticker:
            self.t212_ticker = f"{self.ticker}_US_EQ"


@dataclass
class Position:
    ticker: str
    quantity: float
    buy_price: float
    stop_loss_pct: float
    take_profit_pct: float
    t212_order_id: Optional[int] = None
    bought_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    @property
    def stop_loss_price(self) -> float:
        return self.buy_price * (1 - self.stop_loss_pct / 100)

    @property
    def take_profit_price(self) -> float:
        return self.buy_price * (1 + self.take_profit_pct / 100)


@dataclass
class Trade:
    ticker: str
    side: str
    quantity: float
    price: float
    reason: str
    pnl: Optional[float] = None
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class LogEntry:
    tr: str                  # Türkçe mesaj
    en: str                  # İngilizce mesaj
    level: str = "info"
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


# Satış sebeplerinin çevirileri
_REASON_TR = {"signal": "Sinyal", "stop_loss": "Zarar Kes", "take_profit": "Kâr Al", "manual": "Manuel"}
_REASON_EN = {"signal": "Signal", "stop_loss": "Stop Loss",  "take_profit": "Take Profit", "manual": "Manual"}


class Bot:
    def __init__(self, t212: Optional["Trading212Client"], feed: PriceFeed):
        self.t212 = t212
        self.feed = feed
        self.stocks: Dict[str, StockConfig] = {}
        self.positions: Dict[str, Position] = {}
        self.reference_prices: Dict[str, float] = {}
        self.reference_dates: Dict[str, date] = {}
        self.current_prices: Dict[str, Optional[float]] = {}
        self.trades: List[Trade] = []
        self.logs: List[LogEntry] = []
        self.running = False
        self._task: Optional[asyncio.Task] = None

    # ---------- Stock management ----------

    def add_stock(self, config: StockConfig):
        self.stocks[config.ticker] = config
        self.current_prices.setdefault(config.ticker, None)
        self._log(
            f"{config.ticker} izleme listesine eklendi",
            en=f"{config.ticker} added to watchlist",
        )
        self.save_state()

    def remove_stock(self, ticker: str):
        self.stocks.pop(ticker, None)
        self.current_prices.pop(ticker, None)
        self._log(
            f"{ticker} izleme listesinden çıkarıldı",
            en=f"{ticker} removed from watchlist",
        )
        self.save_state()

    def update_stock(self, ticker: str, **kwargs):
        if ticker in self.stocks:
            for k, v in kwargs.items():
                if hasattr(self.stocks[ticker], k):
                    setattr(self.stocks[ticker], k, v)
            self._log(
                f"{ticker} ayarları güncellendi",
                en=f"{ticker} settings updated",
            )
            self.save_state()

    # ---------- Bot lifecycle ----------

    async def start(self):
        if self.running:
            return
        if self.t212 is None:
            self._log("T212 bağlı değil, bot başlatılamaz", level="error", en="T212 not connected, cannot start bot")
            return
        self.running = True
        self._log("Bot başlatıldı ✓", en="Bot started ✓")
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._log("Bot durduruldu", en="Bot stopped")

    async def manual_tick(self):
        await self._tick()

    # ---------- Main loop ----------

    async def _run_loop(self):
        while self.running:
            try:
                await self._tick()
            except Exception as e:
                self._log(f"Döngü hatası: {e}", level="error", en=f"Loop error: {e}")
                logger.exception(e)
            await asyncio.sleep(INTERVAL_SECONDS)

    async def _tick(self):
        tasks = [
            self._process_stock(ticker, cfg)
            for ticker, cfg in list(self.stocks.items())
            if cfg.enabled
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---------- Per-stock logic ----------

    async def _process_stock(self, ticker: str, config: StockConfig):
        quote = await self.feed.get_quote(ticker)
        if quote is None:
            self._log(
                f"{ticker}: fiyat alınamadı", level="warning",
                en=f"{ticker}: price unavailable",
            )
            return
        price = quote["price"]
        self.current_prices[ticker] = price

        today = date.today()
        if self.reference_dates.get(ticker) != today and quote.get("open"):
            self.reference_prices[ticker] = quote["open"]
            self.reference_dates[ticker] = today
            self._log(
                f"{ticker}: gün açılışı = ${quote['open']:.4f}",
                en=f"{ticker}: day open = ${quote['open']:.4f}",
            )
        elif self.reference_dates.get(ticker) != today:
            await self._refresh_reference(ticker)

        ref = self.reference_prices.get(ticker)
        if ref is None:
            self._log(
                f"{ticker}: referans fiyat yok, bekleniyor", level="warning",
                en=f"{ticker}: reference price unavailable, waiting",
            )
            return

        drop_from_ref = (ref - price) / ref * 100

        if ticker in self.positions:
            await self._check_exit(ticker, config, price)
        else:
            self._log(
                f"{ticker} | fiyat: ${price:.4f} | referans: ${ref:.4f} | düşüş: {drop_from_ref:.2f}%",
                en=f"{ticker} | price: ${price:.4f} | ref: ${ref:.4f} | drop: {drop_from_ref:.2f}%",
            )
            if drop_from_ref >= config.buy_drop_pct:
                qty = int(config.max_investment / price)
                if qty > 0:
                    self._log(
                        f"{ticker}: %{drop_from_ref:.1f} düştü → alım sinyali ({qty} adet @ ${price:.4f})",
                        en=f"{ticker}: dropped {drop_from_ref:.1f}% → buy signal ({qty} shares @ ${price:.4f})",
                    )
                    await self._buy(ticker, config, qty, price)
                else:
                    self._log(
                        f"{ticker}: fiyat ${price:.4f} çok yüksek, ${config.max_investment} ile 1 adet bile alınamıyor",
                        level="warning",
                        en=f"{ticker}: price ${price:.4f} too high, cannot buy even 1 share with ${config.max_investment}",
                    )

    async def _check_exit(self, ticker: str, config: StockConfig, price: float):
        pos = self.positions[ticker]
        gain_pct = (price - pos.buy_price) / pos.buy_price * 100

        self._log(
            f"{ticker}: pozisyon açık | alış ${pos.buy_price:.4f} | şu an ${price:.4f} | {gain_pct:+.2f}%",
            en=f"{ticker}: position open | entry ${pos.buy_price:.4f} | now ${price:.4f} | {gain_pct:+.2f}%",
        )

        if gain_pct <= -pos.stop_loss_pct:
            self._log(
                f"{ticker}: ZARAR KES tetiklendi ({gain_pct:.2f}%)", level="warning",
                en=f"{ticker}: STOP LOSS triggered ({gain_pct:.2f}%)",
            )
            await self._sell(ticker, pos, "stop_loss", price)
        elif gain_pct >= pos.take_profit_pct:
            self._log(
                f"{ticker}: KÂR AL tetiklendi (+{gain_pct:.2f}%)",
                en=f"{ticker}: TAKE PROFIT triggered (+{gain_pct:.2f}%)",
            )
            await self._sell(ticker, pos, "take_profit", price)

    async def _refresh_reference(self, ticker: str):
        today = date.today()
        if self.reference_dates.get(ticker) != today:
            ref = await self.feed.get_day_open(ticker)
            if ref:
                self.reference_prices[ticker] = ref
                self.reference_dates[ticker] = today
                self._log(
                    f"{ticker}: gün açılışı = ${ref:.4f}",
                    en=f"{ticker}: day open = ${ref:.4f}",
                )

    # ---------- Order execution ----------

    async def _buy(self, ticker: str, config: StockConfig, qty: float, price: float):
        if self.t212 is None:
            self._log(f"{ticker}: T212 bağlı değil, alım atlandı", level="error", en=f"{ticker}: T212 not connected, buy skipped")
            return
        try:
            result = await self.t212.place_market_order(config.t212_ticker, qty)
            order_id = result.get("id") if result else None
            self.positions[ticker] = Position(
                ticker=ticker,
                quantity=qty,
                buy_price=price,
                stop_loss_pct=config.stop_loss_pct,
                take_profit_pct=config.take_profit_pct,
                t212_order_id=order_id,
            )
            self.trades.append(
                Trade(ticker=ticker, side="BUY", quantity=qty, price=price, reason="signal")
            )
            self._log(
                f"{ticker}: ALINDI {qty} adet @ ${price:.4f} (${qty * price:.2f}) | emir #{order_id}",
                en=f"{ticker}: BOUGHT {qty} shares @ ${price:.4f} (${qty * price:.2f}) | order #{order_id}",
            )
            self.save_state()
        except Trading212Error as e:
            self._log(
                f"{ticker}: alım emri başarısız — {e}", level="error",
                en=f"{ticker}: buy order failed — {e}",
            )

    async def _sell(self, ticker: str, pos: Position, reason: str, price: float):
        if self.t212 is None:
            self._log(f"{ticker}: T212 bağlı değil, satım atlandı", level="error", en=f"{ticker}: T212 not connected, sell skipped")
            return
        try:
            result = await self.t212.place_market_order(
                self.stocks[ticker].t212_ticker, -pos.quantity
            )
            order_id = result.get("id") if result else None
            pnl = round((price - pos.buy_price) * pos.quantity, 2)
            self.trades.append(
                Trade(ticker=ticker, side="SELL", quantity=pos.quantity,
                      price=price, reason=reason, pnl=pnl)
            )
            del self.positions[ticker]
            reason_tr = _REASON_TR.get(reason, reason)
            reason_en = _REASON_EN.get(reason, reason)
            self._log(
                f"{ticker}: SATILDI {pos.quantity} adet @ ${price:.4f} | P&L: ${pnl:+.2f} | sebep: {reason_tr} | emir #{order_id}",
                en=f"{ticker}: SOLD {pos.quantity} shares @ ${price:.4f} | P&L: ${pnl:+.2f} | reason: {reason_en} | order #{order_id}",
            )
            self.save_state()
        except Trading212Error as e:
            self._log(
                f"{ticker}: satım emri başarısız — {e}", level="error",
                en=f"{ticker}: sell order failed — {e}",
            )

    # ---------- Logging ----------

    def _log(self, tr: str, level: str = "info", en: str = ""):
        entry = LogEntry(tr=tr, en=en or tr, level=level)
        self.logs.append(entry)
        if len(self.logs) > 500:
            self.logs = self.logs[-500:]
        getattr(logger, level, logger.info)(tr)

    # ---------- State snapshot ----------

    def get_state(self) -> dict:
        stocks_out = {}
        for ticker, cfg in self.stocks.items():
            pos   = self.positions.get(ticker)
            ref   = self.reference_prices.get(ticker)
            price = self.current_prices.get(ticker)

            drop_from_ref = None
            gain_pct      = None

            if ref and price:
                drop_from_ref = round((ref - price) / ref * 100, 2)
            if pos and price:
                gain_pct = round((price - pos.buy_price) / pos.buy_price * 100, 2)

            stocks_out[ticker] = {
                **asdict(cfg),
                "current_price":    price,
                "reference_price":  ref,
                "drop_from_ref_pct": drop_from_ref,
                "position": (
                    {
                        **asdict(pos),
                        "gain_pct":          gain_pct,
                        "stop_loss_price":   round(pos.stop_loss_price, 4),
                        "take_profit_price": round(pos.take_profit_price, 4),
                        "unrealized_pnl": (
                            round((price - pos.buy_price) * pos.quantity, 2) if price else None
                        ),
                    }
                    if pos else None
                ),
            }

        return {
            "running": self.running,
            "stocks":  stocks_out,
            "trades":  [asdict(t) for t in self.trades[-100:]],
            "logs":    [asdict(e) for e in self.logs[-150:]],
        }

    # ---------- Persistence ----------

    def save_state(self):
        today = date.today().isoformat()
        data = {
            "stocks":    {k: asdict(v) for k, v in self.stocks.items()},
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "trades":    [asdict(t) for t in self.trades],
            "reference_prices": {
                k: v for k, v in self.reference_prices.items()
                if self.reference_dates.get(k) == date.today()
            },
            "reference_date": today,
        }
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"State kaydedilemedi: {e}")

    def load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            for ticker, cfg in data.get("stocks", {}).items():
                self.stocks[ticker] = StockConfig(**cfg)
                self.current_prices.setdefault(ticker, None)

            for ticker, pos in data.get("positions", {}).items():
                self.positions[ticker] = Position(**pos)

            for t in data.get("trades", []):
                self.trades.append(Trade(**t))

            today = date.today().isoformat()
            if data.get("reference_date") == today:
                for ticker, price in data.get("reference_prices", {}).items():
                    self.reference_prices[ticker] = price
                    self.reference_dates[ticker] = date.today()

            n_pos = len(self.positions)
            self._log(
                f"State yüklendi — {len(self.stocks)} hisse, {n_pos} açık pozisyon, {len(self.trades)} işlem",
                en=f"State loaded — {len(self.stocks)} stocks, {n_pos} open position(s), {len(self.trades)} trade(s)",
            )
        except Exception as e:
            logger.error(f"State yüklenemedi: {e}")

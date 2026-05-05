import asyncio
import base64
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class Trading212Error(Exception):
    pass


class Trading212Client:
    def __init__(self, api_key: str, api_secret: str = "", demo: bool = True):
        self.base_url = (
            "https://demo.trading212.com/api/v0"
            if demo
            else "https://live.trading212.com/api/v0"
        )
        self.demo = demo

        if api_secret:
            encoded = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
            self._auth_header = f"Basic {encoded}"
        else:
            self._auth_header = api_key

        self._session: Optional[aiohttp.ClientSession] = None
        self._instruments: List[Dict] = []
        self._instruments_ts: float = 0.0

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": self._auth_header}
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        session = await self._session_get()
        url = f"{self.base_url}{path}"
        try:
            async with session.request(method, url, **kwargs) as resp:
                if resp.status == 429:
                    reset = int(resp.headers.get("x-ratelimit-reset", 10))
                    logger.warning(f"Rate limited — waiting {reset}s")
                    await asyncio.sleep(reset)
                    return await self._request(method, path, **kwargs)
                if not resp.ok:
                    body = await resp.text()
                    raise Trading212Error(f"HTTP {resp.status}: {body}")
                if resp.content_type == "application/json":
                    return await resp.json()
                return None
        except aiohttp.ClientError as e:
            raise Trading212Error(f"Connection error: {e}") from e

    # ---------- Account ----------

    async def get_account_summary(self) -> Dict:
        return await self._request("GET", "/equity/account/summary")

    # ---------- Positions ----------

    async def get_positions(self) -> List[Dict]:
        return await self._request("GET", "/equity/positions")

    # ---------- Orders ----------

    async def get_orders(self) -> List[Dict]:
        return await self._request("GET", "/equity/orders")

    async def get_order(self, order_id: int) -> Dict:
        return await self._request("GET", f"/equity/orders/{order_id}")

    async def place_market_order(self, ticker: str, quantity: float) -> Dict:
        """quantity > 0 = buy, quantity < 0 = sell"""
        return await self._request(
            "POST",
            "/equity/orders/market",
            json={"ticker": ticker, "quantity": quantity},
        )

    async def place_limit_order(
        self,
        ticker: str,
        quantity: float,
        limit_price: float,
        time_validity: str = "DAY",
    ) -> Dict:
        return await self._request(
            "POST",
            "/equity/orders/limit",
            json={
                "ticker": ticker,
                "quantity": quantity,
                "limitPrice": limit_price,
                "timeValidity": time_validity,
            },
        )

    async def cancel_order(self, order_id: int) -> None:
        await self._request("DELETE", f"/equity/orders/{order_id}")

    # ---------- History ----------

    async def get_order_history(self, limit: int = 50) -> Dict:
        return await self._request(
            "GET", f"/equity/history/orders?limit={limit}"
        )

    async def get_transactions(self, limit: int = 50) -> Dict:
        return await self._request(
            "GET", f"/equity/history/transactions?limit={limit}"
        )

    # ---------- Metadata ----------

    async def get_instruments(self) -> List[Dict]:
        if not self._instruments or (time.time() - self._instruments_ts) > 21600:
            self._instruments = await self._request("GET", "/equity/metadata/instruments")
            self._instruments_ts = time.time()
        return self._instruments

    async def find_instrument(self, ticker: str) -> Optional[str]:
        """Return the T212 ticker string for a given symbol, or None if not found."""
        instruments = await self.get_instruments()
        ticker_upper = ticker.upper()
        # Prefer exact ticker prefix match (e.g. "SYRE" matches "SYRE_US_EQ")
        for inst in instruments:
            t = inst.get("ticker", "")
            if t.upper().startswith(ticker_upper + "_") or t.upper() == ticker_upper:
                return t
        # Fallback: match by shortName / name
        ticker_lower = ticker.lower()
        for inst in instruments:
            name = (inst.get("shortName") or inst.get("name") or "").lower()
            if ticker_lower == name:
                return inst.get("ticker")
        return None

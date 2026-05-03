"""HyperliquidClient: wrapper async sobre la API pública de Hyperliquid.

Solo endpoints de read necesarios para el copy-bot dry-run:
  - meta: info de assets (universe, maxLeverage)
  - all_mids: precios actuales de todos los perps
  - user_fills: fills de cualquier wallet (público — base del copy-trading)
  - clearinghouse_state: estado de cuenta (margin, posiciones)
  - candles: histórico para SL/TP

Endpoint base: https://api.hyperliquid.xyz/info (POST con JSON body)
NO requiere auth para reads públicos. Sin geo-block desde AR (verificado).
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.hyperliquid.xyz"
TIMEOUT = 10.0


class HyperliquidClient:
    """Async client. Usar como context manager para reusar conexión."""

    def __init__(self, host: str = API_BASE, timeout: float = TIMEOUT) -> None:
        self.host = host.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self.timeout = timeout

    async def __aenter__(self) -> "HyperliquidClient":
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _post_info(self, payload: dict) -> Any:
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        owns = self._client is None
        try:
            r = await client.post(f"{self.host}/info", json=payload)
            r.raise_for_status()
            return r.json()
        finally:
            if owns:
                await client.aclose()

    async def meta(self) -> dict:
        """Info del universe (assets disponibles, leverage máximo)."""
        return await self._post_info({"type": "meta"})

    async def all_mids(self) -> dict[str, str]:
        """Precios mid actuales: {coin: price_str}."""
        return await self._post_info({"type": "allMids"})

    async def user_fills(self, wallet: str, limit: int = 100) -> list[dict]:
        """Fills recientes de un wallet (cualquier wallet pública).

        Cada fill: {coin, dir, sz, px, time, oid, tid, hash, leverage?}
        dir = 'Open Long' | 'Open Short' | 'Close Long' | 'Close Short' | 'Buy' | 'Sell'
        """
        data = await self._post_info({"type": "userFills", "user": wallet})
        if not isinstance(data, list):
            return []
        return data[:limit]

    async def clearinghouse_state(self, wallet: str) -> dict:
        """Estado actual de cuenta: marginSummary + assetPositions."""
        return await self._post_info({"type": "clearinghouseState", "user": wallet})

    async def l2_book(self, coin: str) -> dict:
        """L2 orderbook para un coin. Devuelve {coin, time, levels: [bids, asks]}.

        Cada level: {px, sz, n}. Usado para production-parity fill price
        (walk levels al size que vamos a tomar)."""
        return await self._post_info({"type": "l2Book", "coin": coin})

    async def meta_and_asset_ctxs(self) -> Any:
        """Devuelve [meta, [assetCtx1, ...]] donde cada assetCtx tiene
        funding rate, oraclePx, openInterest, premium, etc.

        Usado para funding accrual hourly.
        """
        return await self._post_info({"type": "metaAndAssetCtxs"})

    async def candles(self, coin: str, interval: str = "1m", lookback_seconds: int = 300) -> list[dict]:
        """Histórico de candles para análisis de precio reciente."""
        now_ms = int(time.time() * 1000)
        return await self._post_info({
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": now_ms - lookback_seconds * 1000,
                "endTime": now_ms,
            },
        })

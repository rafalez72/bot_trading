"""dYdX v4 indexer client (read-only, async).

API: https://indexer.dydx.trade/v4/
Endpoints públicos sin auth ni geo-block. Para dry-run del copy-bot
necesitamos solo reads (fills, markets, orderbook).

Direcciones dYdX usan formato Cosmos: `dydx1<bech32>...` (no 0x...).
Subaccount default = 0 para usuarios normales.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://indexer.dydx.trade/v4"
TIMEOUT = 10.0


class DydxClient:
    """Async client for dYdX v4 indexer (read-only)."""

    def __init__(self, host: str = API_BASE, timeout: float = TIMEOUT) -> None:
        self.host = host.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "DydxClient":
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: dict | None = None) -> Any:
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        owns = self._client is None
        try:
            r = await client.get(f"{self.host}{path}", params=params)
            r.raise_for_status()
            return r.json()
        finally:
            if owns:
                await client.aclose()

    async def perpetual_markets(self) -> dict:
        """All available perp markets with metadata."""
        return await self._get("/perpetualMarkets")

    async def fills(self, wallet: str, subaccount: int = 0, limit: int = 100) -> list[dict]:
        """Fills (trade executions) for a given wallet's subaccount."""
        data = await self._get("/fills", params={
            "address": wallet,
            "subaccountNumber": subaccount,
            "limit": limit,
        })
        return data.get("fills", []) if isinstance(data, dict) else []

    async def positions(self, wallet: str, subaccount: int = 0) -> list[dict]:
        """Open perpetual positions for a wallet."""
        data = await self._get(
            f"/addresses/{wallet}/subaccountNumber/{subaccount}",
        )
        if not isinstance(data, dict):
            return []
        sa = data.get("subaccount", {}) if isinstance(data.get("subaccount"), dict) else {}
        positions_dict = sa.get("openPerpetualPositions", {}) or {}
        return list(positions_dict.values()) if isinstance(positions_dict, dict) else []

    async def orderbook(self, ticker: str) -> dict:
        """Orderbook for a perp market (e.g. 'BTC-USD')."""
        return await self._get(f"/orderbooks/perpetualMarket/{ticker}")

    async def trades(self, ticker: str, limit: int = 100) -> list[dict]:
        """Recent public trades on a market (anyone's trades)."""
        data = await self._get(f"/trades/perpetualMarket/{ticker}", params={"limit": limit})
        return data.get("trades", []) if isinstance(data, dict) else []

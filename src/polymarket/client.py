"""Cliente HTTP para las APIs públicas de Polymarket.

Endpoints utilizados:
- Gamma API   → metadata de mercados, eventos, tags
- Data API    → trades históricos, posiciones, leaderboards
- CLOB API    → order book en vivo (no usado en Fase 1)
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.config import DATA_API, GAMMA_API

log = logging.getLogger(__name__)


def _should_retry(exc: BaseException) -> bool:
    """Retry sólo errores transitorios (red + 5xx + 429)."""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code == 429
    return False


class PolymarketClient:
    def __init__(self, timeout: float = 30.0) -> None:
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "polymarket-copybot/0.1"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "PolymarketClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    @retry(
        retry=retry_if_exception(_should_retry),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _get(self, url: str, params: dict | None = None) -> Any:
        r = await self._http.get(url, params=params)
        r.raise_for_status()
        return r.json()

    # -------- Gamma API: mercados / eventos --------
    async def list_markets(
        self,
        *,
        active: bool | None = None,
        closed: bool | None = None,
        limit: int = 500,
        offset: int = 0,
        order: str | None = None,
        ascending: bool | None = None,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()
        if order is not None:
            params["order"] = order
        if ascending is not None:
            params["ascending"] = str(ascending).lower()
        if end_date_min is not None:
            params["end_date_min"] = end_date_min
        if end_date_max is not None:
            params["end_date_max"] = end_date_max
        return await self._get(f"{GAMMA_API}/markets", params=params)

    async def iter_markets(
        self,
        *,
        page_size: int = 500,
        active: bool | None = None,
        closed: bool | None = None,
        order: str | None = None,
        ascending: bool | None = None,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
    ) -> AsyncIterator[dict]:
        offset = 0
        while True:
            try:
                page = await self.list_markets(
                    active=active, closed=closed, limit=page_size, offset=offset,
                    order=order, ascending=ascending,
                    end_date_min=end_date_min, end_date_max=end_date_max,
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (400, 422):
                    log.warning("iter_markets: offset=%d → %d, fin", offset, e.response.status_code)
                    return
                raise
            if not page:
                return
            for m in page:
                yield m
            if len(page) < page_size:
                return
            offset += page_size

    async def get_market(self, condition_id: str) -> dict | None:
        """Busca un mercado por conditionId. La Gamma API rechaza el path
        /markets/{conditionId} (requiere ID numérico interno), así que
        usamos el query param.
        """
        data = await self._get(
            f"{GAMMA_API}/markets",
            params={"conditionId": condition_id, "limit": 1},
        )
        if isinstance(data, list):
            return data[0] if data else None
        return data if isinstance(data, dict) else None

    # -------- Data API: trades / actividad --------
    async def trades(
        self,
        *,
        user: str | None = None,
        market: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]:
        """Trades históricos. Si `user` es None, devuelve trades globales."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if user:
            params["user"] = user.lower()
        if market:
            params["market"] = market
        return await self._get(f"{DATA_API}/trades", params=params)

    async def iter_user_trades(
        self, user: str, *, page_size: int = 500
    ) -> AsyncIterator[dict]:
        """Pagina por offset hasta el límite duro de la API (~3500).

        Cuando la API responde 400, lo tratamos como fin de stream:
        recolectamos los trades más recientes (suficiente para métricas).
        """
        offset = 0
        while True:
            try:
                page = await self.trades(user=user, limit=page_size, offset=offset)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400:
                    log.warning(
                        "user=%s: offset=%d devolvió 400, fin de stream",
                        user, offset,
                    )
                    return
                raise
            if not page:
                return
            for t in page:
                yield t
            if len(page) < page_size:
                return
            offset += page_size

    async def positions(self, user: str) -> list[dict]:
        return await self._get(
            f"{DATA_API}/positions", params={"user": user.lower()}
        )

    async def value(self, user: str) -> dict:
        """Valor actual del portafolio del wallet."""
        return await self._get(
            f"{DATA_API}/value", params={"user": user.lower()}
        )

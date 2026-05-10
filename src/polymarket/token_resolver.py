"""Resolver consistente ``slug | conditionId`` → ``token_id`` (CLOB).

Bloqueador para wiring live de las estrategias A/B/C/D: cada una necesita
mapear el ``slug`` del market (humano-legible) al ``token_id`` del outcome
YES/NO en el CLOB para enviar órdenes.

Diseño
------
- Cache LRU in-process con TTL 60min — los ``token_ids`` no cambian para
  un market dado (son derivados del ``conditionId`` por outcome).
- Workaround del bug Gamma: pedir ``/markets?conditionId=X`` con
  ``closed=true`` ocasionalmente devuelve markets random. Validamos que
  ``m.conditionId == cid`` antes de aceptar la respuesta.
- Helper ``parse_clob_token_ids`` normaliza el campo ``clobTokenIds`` que
  Gamma serializa inconsistentemente (JSON string a veces, list otras).

API pública
-----------
- ``resolve_token_id(client, slug, outcome_index=0)``
- ``resolve_token_id_by_condition_id(client, condition_id, outcome_index=0)``
- ``parse_clob_token_ids(raw)``
- ``clear_cache()`` — útil en tests
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# TTL del cache: 60min. Los token_ids no cambian para un market.
_CACHE_TTL_S = 60 * 60

# Cache in-process. key = ("slug", slug) | ("cid", condition_id).
# value = (expires_at_ts, token_ids_list).
_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}


def parse_clob_token_ids(clob_token_ids_raw: Any) -> list[str]:
    """Normaliza ``clobTokenIds`` de gamma a ``list[str]``.

    Gamma serializa inconsistente: a veces es JSON string, a veces list.
    Items malformados / no-string-coercibles → ``[]``.
    """
    if clob_token_ids_raw is None:
        return []
    raw = clob_token_ids_raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            raw = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            log.warning("parse_clob_token_ids: JSON inválido %r", clob_token_ids_raw)
            return []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if item is None:
            continue
        try:
            s = str(item).strip()
        except Exception:
            continue
        if s:
            out.append(s)
    return out


def _cache_get(key: tuple[str, str]) -> list[str] | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    expires_at, ids = entry
    if expires_at < time.monotonic():
        # Expirado: drop y miss.
        _cache.pop(key, None)
        return None
    return ids


def _cache_put(key: tuple[str, str], ids: list[str]) -> None:
    _cache[key] = (time.monotonic() + _CACHE_TTL_S, ids)


def clear_cache() -> None:
    """Limpia el cache in-process. Útil en tests."""
    _cache.clear()


def _pick(token_ids: list[str], outcome_index: int) -> str | None:
    if not token_ids:
        return None
    if outcome_index < 0 or outcome_index >= len(token_ids):
        log.warning(
            "token_resolver: outcome_index=%d fuera de rango (n=%d)",
            outcome_index, len(token_ids),
        )
        return None
    return token_ids[outcome_index]


async def _fetch_by_slug(client: Any, slug: str) -> list[str] | None:
    """Pide ``/markets?slug=X``. Devuelve token_ids o ``None`` si no existe.

    Usa ``client._get`` para reutilizar retry/backoff existente.
    """
    from src.config import GAMMA_API

    try:
        data = await client._get(
            f"{GAMMA_API}/markets",
            params={"slug": slug, "limit": 1},
        )
    except Exception as e:
        log.warning("token_resolver: fetch_by_slug(%s) falló: %s", slug, e)
        return None

    market = None
    if isinstance(data, list):
        market = data[0] if data else None
    elif isinstance(data, dict):
        market = data
    if not market:
        return None
    return parse_clob_token_ids(market.get("clobTokenIds"))


async def _fetch_by_condition_id(client: Any, condition_id: str) -> list[str] | None:
    """Pide ``/markets?conditionId=X`` con workaround del bug Gamma.

    Bug: con ``closed=true`` Gamma devuelve markets random a veces. Pedimos
    sin filtro ``closed`` primero; si no hay match exacto por conditionId,
    intentamos con ``closed=true`` y validamos ``m.conditionId == cid``.
    """
    from src.config import GAMMA_API

    async def _call(extra_params: dict[str, Any]) -> dict | None:
        params = {"conditionId": condition_id, "limit": 5}
        params.update(extra_params)
        try:
            data = await client._get(f"{GAMMA_API}/markets", params=params)
        except Exception as e:
            log.warning(
                "token_resolver: fetch_by_cid(%s, %s) falló: %s",
                condition_id, extra_params, e,
            )
            return None
        if isinstance(data, list):
            for m in data:
                if isinstance(m, dict) and m.get("conditionId") == condition_id:
                    return m
            return None
        if isinstance(data, dict):
            if data.get("conditionId") == condition_id:
                return data
            return None
        return None

    # Intento 1: sin filtro closed (markets activos).
    market = await _call({})
    # Intento 2: fallback closed=true para markets resueltos. Validamos cid.
    if market is None:
        market = await _call({"closed": "true"})

    if market is None:
        log.warning(
            "token_resolver: condition_id=%s no encontrado o cid mismatch",
            condition_id,
        )
        return None
    return parse_clob_token_ids(market.get("clobTokenIds"))


async def resolve_token_id(
    client: Any,
    slug: str,
    outcome_index: int = 0,
) -> str | None:
    """Resuelve ``slug → token_id`` con cache (TTL 60min).

    Args:
        client: ``PolymarketClient`` (o duck-type con ``_get``).
        slug: slug del market en Gamma.
        outcome_index: 0=YES (Up), 1=NO (Down) en mercados binarios.

    Returns:
        ``token_id`` (str) o ``None`` si no encontrado / malformed.
    """
    if not slug:
        return None
    key = ("slug", slug)
    cached = _cache_get(key)
    if cached is not None:
        log.debug("token_resolver: cache HIT slug=%s", slug)
        return _pick(cached, outcome_index)

    ids = await _fetch_by_slug(client, slug)
    if ids is None:
        return None
    if not ids:
        log.warning("token_resolver: slug=%s sin clobTokenIds parseables", slug)
        # Cacheamos vacío también — evita tormenta de retries en markets
        # que genuinamente no tienen token_ids (e.g., closed garbage).
        _cache_put(key, [])
        return None
    _cache_put(key, ids)
    return _pick(ids, outcome_index)


async def resolve_token_id_by_condition_id(
    client: Any,
    condition_id: str,
    outcome_index: int = 0,
) -> str | None:
    """Idem ``resolve_token_id`` pero por ``conditionId``.

    Algunos call sites (e.g. positions stream) sólo conocen el cid.
    """
    if not condition_id:
        return None
    key = ("cid", condition_id)
    cached = _cache_get(key)
    if cached is not None:
        log.debug("token_resolver: cache HIT cid=%s", condition_id)
        return _pick(cached, outcome_index)

    ids = await _fetch_by_condition_id(client, condition_id)
    if ids is None:
        return None
    if not ids:
        log.warning(
            "token_resolver: cid=%s sin clobTokenIds parseables", condition_id,
        )
        _cache_put(key, [])
        return None
    _cache_put(key, ids)
    return _pick(ids, outcome_index)

"""Tests para src/polymarket/token_resolver.py.

Cubre:
  1. ``parse_clob_token_ids``: list, JSON string, None, malformed.
  2. ``resolve_token_id`` happy path por slug — mock client._get.
  3. Cache hit: segunda llamada NO golpea API.
  4. ``resolve_token_id_by_condition_id`` con fallback ``closed=true``
     y validación ``m.conditionId == cid``.
  5. ``None`` cuando Gamma devuelve garbage (cid mismatch en ambos intentos).

Mock: ``AsyncMock`` sobre ``client._get`` (no toca httpx real).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.polymarket import token_resolver as tr


@pytest.fixture(autouse=True)
def _clear_cache_before_each():
    """Cada test arranca con cache limpio para evitar cross-talk."""
    tr.clear_cache()
    yield
    tr.clear_cache()


# ---------- Test 1: parse_clob_token_ids variantes ----------

class TestParseClobTokenIds:
    def test_list_passthrough(self):
        assert tr.parse_clob_token_ids(["abc", "def"]) == ["abc", "def"]

    def test_json_string(self):
        assert tr.parse_clob_token_ids('["t1","t2"]') == ["t1", "t2"]

    def test_none_returns_empty(self):
        assert tr.parse_clob_token_ids(None) == []

    def test_malformed_json_returns_empty(self):
        assert tr.parse_clob_token_ids("not-json{{{") == []

    def test_empty_string_returns_empty(self):
        assert tr.parse_clob_token_ids("") == []
        assert tr.parse_clob_token_ids("   ") == []

    def test_non_list_json_returns_empty(self):
        # JSON válido pero no es list (e.g., dict).
        assert tr.parse_clob_token_ids('{"foo": "bar"}') == []

    def test_coerce_numeric_items_to_str(self):
        # Polymarket a veces devuelve token_ids como números muy grandes.
        assert tr.parse_clob_token_ids([123, 456]) == ["123", "456"]

    def test_skip_none_and_empty_items(self):
        assert tr.parse_clob_token_ids(["a", None, "", "b"]) == ["a", "b"]


# ---------- Test 2: resolve_token_id por slug — happy path ----------

@pytest.mark.asyncio
async def test_resolve_token_id_by_slug_happy_path():
    client = AsyncMock()
    client._get = AsyncMock(return_value=[
        {
            "slug": "btc-up-or-down-may-10",
            "conditionId": "0xCID",
            "clobTokenIds": '["TOKEN_YES_123","TOKEN_NO_456"]',
        }
    ])

    yes = await tr.resolve_token_id(client, "btc-up-or-down-may-10", 0)
    no = await tr.resolve_token_id(client, "btc-up-or-down-may-10", 1)

    assert yes == "TOKEN_YES_123"
    assert no == "TOKEN_NO_456"
    # Sólo 1 fetch — segunda llamada usó cache (mismo slug).
    assert client._get.await_count == 1


# ---------- Test 3: cache hit no llama API ----------

@pytest.mark.asyncio
async def test_cache_hit_skips_api_call():
    client = AsyncMock()
    client._get = AsyncMock(return_value=[
        {
            "slug": "some-slug",
            "conditionId": "0xCID",
            "clobTokenIds": ["TID_A", "TID_B"],
        }
    ])

    first = await tr.resolve_token_id(client, "some-slug", 0)
    assert first == "TID_A"
    assert client._get.await_count == 1

    # Segunda llamada al mismo slug — debería ser cache HIT, NO call.
    second = await tr.resolve_token_id(client, "some-slug", 0)
    third = await tr.resolve_token_id(client, "some-slug", 1)
    assert second == "TID_A"
    assert third == "TID_B"
    assert client._get.await_count == 1, "Cache hit no debe golpear API"


# ---------- Test 4: fallback conditionId con closed=true + cid validation ----------

@pytest.mark.asyncio
async def test_resolve_by_condition_id_fallback_closed_true():
    """Primer intento (sin closed) devuelve [] / no match. Segundo intento
    con closed=true devuelve el market correcto y validamos conditionId.
    """
    client = AsyncMock()
    cid_target = "0xABCDEF"

    call_history = []

    async def fake_get(url, params=None):
        call_history.append(dict(params or {}))
        # 1er call: sin "closed" → [] (market está closed, no aparece).
        if "closed" not in (params or {}):
            return []
        # 2do call: closed=true → devuelve el market correcto.
        return [
            {
                "conditionId": cid_target,
                "slug": "resolved-market",
                "clobTokenIds": ["RES_YES", "RES_NO"],
            }
        ]

    client._get = AsyncMock(side_effect=fake_get)

    tid = await tr.resolve_token_id_by_condition_id(client, cid_target, 0)

    assert tid == "RES_YES"
    # Debió hacer DOS calls: primero sin closed, después closed=true.
    assert client._get.await_count == 2
    assert "closed" not in call_history[0]
    assert call_history[1].get("closed") == "true"
    assert call_history[1].get("conditionId") == cid_target


# ---------- Test 5: garbage cid mismatch → None ----------

@pytest.mark.asyncio
async def test_returns_none_when_gamma_returns_mismatched_cid():
    """Bug Gamma: ?conditionId=X con closed=true a veces devuelve markets
    con OTRO conditionId. Debemos validar y devolver None.
    """
    client = AsyncMock()
    cid_target = "0xWANTED"

    async def fake_get(url, params=None):
        # Ambos intentos devuelven markets random con cid distinto.
        return [
            {
                "conditionId": "0xRANDOM_GARBAGE",
                "slug": "unrelated",
                "clobTokenIds": ["G1", "G2"],
            }
        ]

    client._get = AsyncMock(side_effect=fake_get)

    tid = await tr.resolve_token_id_by_condition_id(client, cid_target, 0)

    assert tid is None
    # Hizo 2 calls (intento sin-closed + fallback closed=true), ambos fallaron
    # validación cid → None.
    assert client._get.await_count == 2


# ---------- Test bonus: outcome_index out of range → None ----------

@pytest.mark.asyncio
async def test_outcome_index_out_of_range_returns_none():
    client = AsyncMock()
    client._get = AsyncMock(return_value=[
        {"slug": "s", "clobTokenIds": ["only-one"]}
    ])

    # outcome_index=1 pero sólo hay 1 token_id.
    tid = await tr.resolve_token_id(client, "s", 1)
    assert tid is None


# ---------- Test bonus: empty slug / cid → None sin API call ----------

@pytest.mark.asyncio
async def test_empty_input_returns_none_without_api_call():
    client = AsyncMock()
    client._get = AsyncMock()

    assert await tr.resolve_token_id(client, "", 0) is None
    assert await tr.resolve_token_id_by_condition_id(client, "", 0) is None
    assert client._get.await_count == 0

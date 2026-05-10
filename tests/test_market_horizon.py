"""Smoke tests para los nuevos filtros de horizon y categorías ultra-short.

Cubre:
- `categorize.is_ultrashort_market`: regex de slugs para crypto/esports/sport.
- `paper.open_position`: rechazo `market_too_short` cuando end_date < 30min.
- `paper.open_position`: rechazo `ultrashort_market` por slug pattern.
- Excepción: `source_wallet='crypto_arb'` saltea ambos filtros.
"""
from __future__ import annotations

import time

from src.copybot import paper
from src.copybot.categorize import is_ultrashort_market
from src.db.schema import db, tx


# ---------- regex de slugs ----------


def test_is_ultrashort_crypto_updown():
    assert is_ultrashort_market("btc-updown-5m-1777505400-et-2pm") is True
    assert is_ultrashort_market("eth-updown-15m-foo") is True
    assert is_ultrashort_market("sol-updown-1h-x") is True
    assert is_ultrashort_market("hype-updown-4h-y") is True
    # 1d no es ultra-short
    assert is_ultrashort_market("btc-updown-1d-x") is False


def test_is_ultrashort_esports():
    assert is_ultrashort_market("cs2-team-a-vs-b") is True
    assert is_ultrashort_market("lol-game-2-2026") is True
    assert is_ultrashort_market("valorant-final-2026") is True
    assert is_ultrashort_market("dota-major-2026") is True
    assert is_ultrashort_market("csgo-foo-2026") is True
    # sin keyword esports
    assert is_ultrashort_market("us-election-trump-2026") is False


def test_is_ultrashort_sport_solo_si_end_date_dentro_6h():
    now = int(time.time())
    assert is_ultrashort_market("nfl-team-vs-other-2026-01-01", None) is False
    # 1 hora hacia adelante: live game
    assert is_ultrashort_market("nfl-team-vs-other-2026-01-01", now + 3600) is True
    # 30 días: no es live game
    assert is_ultrashort_market("nfl-team-vs-other-2026-01-01", now + 30 * 86400) is False
    # past (pero defensivo): no aplicar
    assert is_ultrashort_market("nfl-team-vs-other-2026-01-01", now - 100) is False
    # ufc-sea1 dentro de 30min
    assert is_ultrashort_market("ufc-sea1-fight-2026-foo", now + 1800) is True


def test_is_ultrashort_inputs_raros():
    assert is_ultrashort_market(None) is False
    assert is_ultrashort_market("") is False


# ---------- helpers para el path open_position ----------


def _seed_subscription(wallet: str = "0xtrader") -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO copy_subscriptions (wallet, status, sizing_mult) "
            "VALUES (?, 'active', 1.0) ON CONFLICT(wallet) DO UPDATE SET "
            "status='active', sizing_mult=1.0",
            (wallet,),
        )


def _insert_market(
    *,
    condition_id: str,
    slug: str,
    end_date: str | None,
    liquidity: float = 50000.0,
    volume: float = 50000.0,
    category: str | None = None,
) -> None:
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO markets (
                condition_id, slug, question, category, end_date, active, closed,
                volume, liquidity
            ) VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?)
            ON CONFLICT(condition_id) DO UPDATE SET
                slug=excluded.slug, end_date=excluded.end_date,
                volume=excluded.volume, liquidity=excluded.liquidity,
                category=excluded.category
            """,
            (condition_id, slug, "Q?", category, end_date, volume, liquidity),
        )


def _iso(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- Feature A ----------


def test_market_too_short_rechaza(isolated_db, monkeypatch):
    """end_date dentro de 2 min → reject market_too_short.

    Default actualizado 2026-05-10: MARKET_HORIZON_MIN_SECS bajado a 300s.
    Test usa 120s (< threshold) para garantizar reject independiente del default.
    """
    monkeypatch.setattr(paper, "MAX_TRADE_AGE_SECONDS", 10**9)
    _seed_subscription()
    now = int(time.time())
    _insert_market(
        condition_id="0xshort",
        slug="some-news-event-2026",
        end_date=_iso(now + 120),  # 2 min < threshold 300s
    )
    pid, reason = paper.open_position(
        source_wallet="0xtrader",
        source_trade_id="t1",
        condition_id="0xshort",
        outcome="Yes",
        outcome_index=0,
        price=0.5,
        timestamp=now,
        raw={"slug": "some-news-event-2026", "asset": "tok"},
    )
    assert pid is None
    assert reason == "market_too_short"


def test_market_horizon_ok_si_end_date_lejos(isolated_db, monkeypatch):
    """end_date a 1 día → no aplica el filtro."""
    monkeypatch.setattr(paper, "MAX_TRADE_AGE_SECONDS", 10**9)
    _seed_subscription()
    now = int(time.time())
    _insert_market(
        condition_id="0xlong",
        slug="some-news-event-2026",
        end_date=_iso(now + 86400),
    )
    pid, reason = paper.open_position(
        source_wallet="0xtrader",
        source_trade_id="t2",
        condition_id="0xlong",
        outcome="Yes",
        outcome_index=0,
        price=0.5,
        timestamp=now,
        raw={"slug": "some-news-event-2026", "asset": "tok"},
    )
    # Puede pasar otros filtros y abrir, o reject por algo distinto a horizon.
    assert reason != "market_too_short"


def test_market_horizon_end_date_null_no_rechaza(isolated_db, monkeypatch):
    """end_date NULL → fail-open (allow)."""
    monkeypatch.setattr(paper, "MAX_TRADE_AGE_SECONDS", 10**9)
    _seed_subscription()
    now = int(time.time())
    _insert_market(
        condition_id="0xnull",
        slug="some-news-event-2026",
        end_date=None,
    )
    pid, reason = paper.open_position(
        source_wallet="0xtrader",
        source_trade_id="t3",
        condition_id="0xnull",
        outcome="Yes",
        outcome_index=0,
        price=0.5,
        timestamp=now,
        raw={"slug": "some-news-event-2026", "asset": "tok"},
    )
    assert reason != "market_too_short"


def test_market_horizon_skip_para_crypto_arb(isolated_db, monkeypatch):
    """crypto_arb está exento — opera markets cortos por diseño."""
    monkeypatch.setattr(paper, "MAX_TRADE_AGE_SECONDS", 10**9)
    _seed_subscription("crypto_arb")
    now = int(time.time())
    _insert_market(
        condition_id="0xshort2",
        slug="btc-updown-5m-foo",
        end_date=_iso(now + 200),  # < 30min
    )
    pid, reason = paper.open_position(
        source_wallet="crypto_arb",
        source_trade_id="t4",
        condition_id="0xshort2",
        outcome="Yes",
        outcome_index=0,
        price=0.5,
        timestamp=now,
        raw={"slug": "btc-updown-5m-foo", "asset": "tok"},
    )
    # crypto_arb se exenta de horizon Y de ultrashort. Cualquier reject
    # restante (slug expiry parser, low_liq, etc.) debe NO ser nuestros.
    assert reason not in ("market_too_short", "ultrashort_market")


# ---------- Feature E ----------


def test_ultrashort_market_rechaza_por_slug(isolated_db, monkeypatch):
    """btc-updown-5m → reject ultrashort_market."""
    monkeypatch.setattr(paper, "MAX_TRADE_AGE_SECONDS", 10**9)
    _seed_subscription()
    now = int(time.time())
    # end_date a 1 día para que NO dispare market_too_short, así aislamos
    # el filtro ultrashort_market.
    _insert_market(
        condition_id="0xcryp",
        slug="btc-updown-5m-1777505400",
        end_date=_iso(now + 86400),
    )
    pid, reason = paper.open_position(
        source_wallet="0xtrader",
        source_trade_id="tu1",
        condition_id="0xcryp",
        outcome="Yes",
        outcome_index=0,
        price=0.5,
        timestamp=now,
        raw={"slug": "btc-updown-5m-1777505400", "asset": "tok"},
    )
    assert pid is None
    assert reason == "ultrashort_market"


# ---------- Feature G ----------


def test_our_entry_columns_se_persisten(isolated_db, monkeypatch):
    """`our_entry_at` y `our_entry_price` se guardan cuando se pasan."""
    monkeypatch.setattr(paper, "MAX_TRADE_AGE_SECONDS", 10**9)
    _seed_subscription()
    now = int(time.time())
    _insert_market(
        condition_id="0xok",
        slug="us-election-2026",
        end_date=_iso(now + 86400),
        liquidity=50000.0,
    )
    pid, reason = paper.open_position(
        source_wallet="0xtrader",
        source_trade_id="tg1",
        condition_id="0xok",
        outcome="Yes",
        outcome_index=0,
        price=0.5,
        timestamp=now - 5,
        raw={"slug": "us-election-2026", "asset": "tok"},
        our_entry_at=now,
        our_entry_price=0.51,
    )
    assert pid is not None, f"expected open, got reason={reason}"
    with db() as conn:
        row = conn.execute(
            "SELECT entry_at, our_entry_at, our_entry_price FROM paper_trades WHERE id=?",
            (pid,),
        ).fetchone()
    assert int(row["entry_at"]) == now - 5
    assert int(row["our_entry_at"]) == now
    assert abs(float(row["our_entry_price"]) - 0.51) < 1e-9


def test_our_entry_default_si_no_se_pasa(isolated_db, monkeypatch):
    """Si no se pasan, our_entry_at default = time.time(); our_entry_price=NULL."""
    monkeypatch.setattr(paper, "MAX_TRADE_AGE_SECONDS", 10**9)
    _seed_subscription()
    now = int(time.time())
    _insert_market(
        condition_id="0xok2",
        slug="us-election-2026",
        end_date=_iso(now + 86400),
        liquidity=50000.0,
    )
    pid, reason = paper.open_position(
        source_wallet="0xtrader",
        source_trade_id="tg2",
        condition_id="0xok2",
        outcome="Yes",
        outcome_index=0,
        price=0.5,
        timestamp=now,
        raw={"slug": "us-election-2026", "asset": "tok"},
    )
    assert pid is not None, f"expected open, got reason={reason}"
    with db() as conn:
        row = conn.execute(
            "SELECT our_entry_at, our_entry_price FROM paper_trades WHERE id=?",
            (pid,),
        ).fetchone()
    assert row["our_entry_at"] is not None
    assert row["our_entry_price"] is None

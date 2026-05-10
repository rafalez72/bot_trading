"""CLV (Closing Line Value) tracker.

Métrica de edge real ortogonal al PnL ruidoso:

    CLV_pct = (closing_price - entry_price) / entry_price        # BUY
    CLV_pct = (entry_price  - closing_price) / entry_price       # SELL

Interpretación:
- CLV > 0  → bot captó edge real: el mercado se movió a favor del side
             que el bot tomó (proxy de "estábamos del lado correcto del
             trade aún si el outcome final no salió").
- CLV < 0  → adversarial fill: el mercado se movió contra el side. Este
             es el síntoma típico de copiar a wallets ya stale o de ser
             el "tonto del table" — incluso wins ocasionales no
             compensan la sangría.
- CLV cercano a 0 → trades sin información: el mercado no se movió tras
                    nuestro entry → estamos comerciando ruido.

Por qué CLV y no PnL:
- Sports betting / mercados predictivos: el PnL realizado tiene varianza
  enorme (binary outcomes) y se necesitan cientos de muestras para
  separar señal del ruido.
- CLV es continuo y converge mucho más rápido al edge verdadero — pro
  bettors lo usan como métrica primaria de evaluación de un sistema.

Uso típico:
    record_clv(trade_id=42, entry_price=0.45, closing_price=1.0,
               side="BUY", source="paper", bucket_slug="will-btc-rise-...")
    summary = compute_clv_summary(window_hours=24)
    text = report_clv()  # → string Telegram-ready

NO modifica las firmas existentes de paper.settle_resolved /
crypto_arb._settle_crypto_arb_resolved — cada uno llama internamente a
`record_clv` por trade settled. Si la inserción falla, el settlement
sigue normal (errores de tracker no bloquean la liquidación).
"""
from __future__ import annotations

import logging
import statistics
import time
from typing import Optional

from src.db.schema import db, tx

log = logging.getLogger(__name__)


def _signed_clv_pct(entry_price: float, closing_price: float, side: str) -> float:
    """Computa el CLV firmado por side.

    BUY:  (closing - entry) / entry → positivo si market subió.
    SELL: (entry - closing) / entry → positivo si market bajó.

    Defensivo: si entry_price <= 0 devuelve 0 (no podemos normalizar).
    """
    if entry_price is None or closing_price is None:
        return 0.0
    if entry_price <= 0:
        return 0.0
    if (side or "").upper() == "SELL":
        return (entry_price - closing_price) / entry_price
    return (closing_price - entry_price) / entry_price


def record_clv(
    trade_id: int,
    entry_price: float,
    closing_price: float,
    *,
    side: str = "BUY",
    source: str = "paper",
    bucket_slug: Optional[str] = None,
) -> Optional[float]:
    """INSERT a clv_metrics. Devuelve el clv_pct calculado (o None si error).

    NO levanta excepciones: el caller (settle hook) no debe romperse si
    la tabla aún no existe (test envs sin migrations) o si la conexión
    falla. Errores → log warning + return None.
    """
    try:
        clv_pct = _signed_clv_pct(entry_price, closing_price, side)
    except Exception:
        log.exception("clv_tracker.record_clv: error computando clv_pct")
        return None

    now = int(time.time())
    try:
        with tx() as conn:
            conn.execute(
                """
                INSERT INTO clv_metrics
                  (trade_id, source, entry_price, closing_price,
                   clv_pct, side, bucket_slug, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id, source, float(entry_price), float(closing_price),
                    float(clv_pct), (side or "BUY").upper(), bucket_slug, now,
                ),
            )
    except Exception:
        log.warning(
            "clv_tracker.record_clv: insert falló trade_id=%s source=%s",
            trade_id, source,
        )
        return None
    return clv_pct


def compute_clv_summary(window_hours: int = 24) -> dict:
    """Agrega CLV en una ventana de tiempo.

    Devuelve dict con:
      - n: cantidad de muestras en la ventana
      - avg_clv_pct: media (positiva = edge real)
      - median_clv_pct: mediana (robusta a outliers de payout 0/1)
      - positive_pct: % de trades con CLV>0 (proxy de hit rate de edge)
      - by_source: dict {source → {n, avg, positive_pct}}

    Si no hay datos en la ventana, devuelve dict con n=0 y campos None.
    """
    cutoff = int(time.time()) - max(0, int(window_hours)) * 3600
    try:
        with db() as conn:
            rows = conn.execute(
                """
                SELECT source, clv_pct
                FROM clv_metrics
                WHERE recorded_at >= ?
                """,
                (cutoff,),
            ).fetchall()
    except Exception:
        log.exception("clv_tracker.compute_clv_summary: query falló")
        return {"n": 0, "avg_clv_pct": None, "median_clv_pct": None,
                "positive_pct": None, "by_source": {}}

    if not rows:
        return {"n": 0, "avg_clv_pct": None, "median_clv_pct": None,
                "positive_pct": None, "by_source": {}}

    # rows puede ser sqlite3.Row, dict, o tupla — normalizamos
    def _get(r, key, idx):
        try:
            return r[key]
        except (KeyError, IndexError, TypeError):
            try:
                return r[idx]
            except Exception:
                return None

    all_clv: list[float] = []
    by_source: dict[str, list[float]] = {}
    for r in rows:
        src = _get(r, "source", 0) or "unknown"
        clv = _get(r, "clv_pct", 1)
        if clv is None:
            continue
        clv_f = float(clv)
        all_clv.append(clv_f)
        by_source.setdefault(src, []).append(clv_f)

    def _agg(vals: list[float]) -> dict:
        if not vals:
            return {"n": 0, "avg_clv_pct": None, "median_clv_pct": None,
                    "positive_pct": None}
        positives = sum(1 for v in vals if v > 0)
        return {
            "n": len(vals),
            "avg_clv_pct": statistics.fmean(vals),
            "median_clv_pct": statistics.median(vals),
            "positive_pct": positives / len(vals),
        }

    summary = _agg(all_clv)
    summary["by_source"] = {s: _agg(v) for s, v in by_source.items()}
    return summary


def report_clv(window_hours: int = 24) -> str:
    """Genera un string Telegram-ready con el summary CLV de las últimas N horas.

    Formato: lineas con emoji-free pero con bullet '·' para legibilidad.
    Si no hay datos → mensaje explícito.
    """
    s = compute_clv_summary(window_hours=window_hours)
    if not s.get("n"):
        return f"CLV ({window_hours}h): sin trades settled"

    lines: list[str] = []
    lines.append(f"CLV ({window_hours}h): n={s['n']}")
    avg = s.get("avg_clv_pct")
    med = s.get("median_clv_pct")
    pos = s.get("positive_pct")
    if avg is not None:
        lines.append(f"  avg={avg*100:+.2f}% median={med*100:+.2f}%")
    if pos is not None:
        lines.append(f"  positive={pos*100:.1f}%")
    by_src = s.get("by_source") or {}
    for src, agg in sorted(by_src.items()):
        if not agg.get("n"):
            continue
        lines.append(
            f"  · {src}: n={agg['n']} "
            f"avg={(agg['avg_clv_pct'] or 0)*100:+.2f}% "
            f"pos={(agg['positive_pct'] or 0)*100:.0f}%"
        )
    return "\n".join(lines)

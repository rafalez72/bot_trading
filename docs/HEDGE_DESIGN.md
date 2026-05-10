# HEDGE_DESIGN — crypto_arb hedgeado con perp Binance

**Fecha**: 2026-05-10
**Status**: research / pre-implementación. NO código todavía.
**Goal**: capturar lag estructural mid Polymarket vs spot Binance, neutralizando dirección con perp short en Binance.

> Caveman style. Frases cortas. Decisiones primero, justificación después.

---

## 0. TL;DR (mira esto primero)

- Hedge **no salva** trades de updown-5m si Polymarket cobra **1.80% taker en crypto** (peak en p=0.50).
- Cuesta roundtrip Polymarket solo: ~**3.6% del notional** (taker entry + taker exit O settlement gratis).
- Cuesta hedge Binance perp: ~**0.10% del notional** (taker x2 = 0.10% + funding ~0%/8h).
- **Total cost ~3.7% del notional comprado en Polymarket**, NO del bet sizing en USDC.
- Edge realista del modelo `edge_vs_mid` típico = **5-12 pp en p**, traducido a USDC = `bet_usdc * edge_pp / mid` → ~10-20% bet sobre $5 = **$0.50-$1**.
- Roundtrip cost sobre $5 bet @ mid 0.55 = `(5/0.55) * 0.55 * 0.018 * (0.55*0.45)` = $0.022 entry + $0 exit (settle) + perp $0.005 = **~$0.027**.
- ESPERA. Hagamos las cuentas con cuidado.
- Conclusión preliminar: **es viable PERO el margen es delgado**. Ver §2.

---

## 1. Architecture (caveman, ASCII)

```
                    ┌─────────────────────┐
                    │  crypto_arb_signals │  ← edge_vs_mid (existing)
                    │  edge_vs_mid()      │
                    └──────────┬──────────┘
                               │
                   decision: side, p_up, edge
                               │
                               ▼
              ┌───────────────────────────────┐
              │   crypto_arb_hedge.py (NEW)   │  ← orchestrator
              │                               │
              │   1. open Polymarket BUY      │
              │   2. open Binance perp SHORT  │
              │      (compute size first!)    │
              │   3. record (pid_pmkt, ord_id_perp)
              │      en DB tabla hedge_pairs  │
              └───────────┬───────────────────┘
                          │
            ┌─────────────┼──────────────┐
            ▼             ▼              ▼
   ┌──────────────┐ ┌────────────┐ ┌──────────────┐
   │ tradebook    │ │ perp_client│ │ perp_account │
   │ (existing)   │ │ NEW signed │ │ NEW balance  │
   │ open_position│ │ market ord │ │ position     │
   └──────────────┘ └────────────┘ └──────────────┘
            │              │              │
            ▼              ▼              ▼
       Polymarket      fapi.binance    fapi.binance
       CLOB            /fapi/v1/order  /fapi/v2/balance
       (gasless via    HMAC-SHA256     /fapi/v2/positionRisk
       0x relayer)     hedge mode

```

Settlement:
```
   bucket end_ts → settler corre cada N seg
                 ↓
    para cada hedge_pair OPEN:
       a. settle Polymarket (esperar gamma closed=true) → $X
       b. close perp via MARKET reduceOnly → $Y
       c. PnL_neto = X + Y - costos
       d. update DB → status=settled
```

Falla parcial → §4.

---

## 2. Costos breakdown con números reales

### 2.1 Polymarket (CRÍTICO)

**Fee formula real (verificada en docs.polymarket.com 2026-04)**:

```
fee_usdc = C × p × feeRate × (p × (1 - p))^exponent
```

Donde:
- `C` = shares = `bet_usdc / p` (porque pagás `p` USDC por share que paga $1 si win)
- `p` = precio de entrada (mid)
- `feeRate` = **0.018 (1.80%)** para Crypto category (incluye btc-updown-5m)
- `exponent` = **1** para Crypto (peak en p=0.50)

**Simplificación**: `fee_usdc = bet_usdc × feeRate × (p × (1-p))`

**Ejemplos**:
| bet_usdc | p (mid) | fee USDC | fee % bet |
|----------|---------|----------|-----------|
| $5       | 0.50    | $0.0450  | 0.90%     |
| $5       | 0.55    | $0.0445  | 0.89%     |
| $5       | 0.60    | $0.0432  | 0.86%     |
| $5       | 0.70    | $0.0378  | 0.76%     |
| $10      | 0.55    | $0.089   | 0.89%     |

**Settlement (exit)**: NO hay fee on win/loss en Polymarket. Solo fee al **buy** (taker).
→ **roundtrip Polymarket = solo el entry fee** (~0.9% del bet en p=0.50-0.60).

**Maker rebate**: 20% del fee redistribuido a makers daily. NOSOTROS somos taker → pagamos 100%.

**Slippage real (NO doc, observado del bot existente)**: orderbooks de updown-5m tienen $1-5k liquidity. Un BUY de $5 mueve ~0.5-1% del book → **slippage adicional 0.5-1pp** en p efectivo.

⚠️ **Combo total entrada Polymarket**:
- Fee: ~0.9% bet
- Slippage: 0.5-1% bet (en peor caso, mid_efectivo > mid_quoted)
- **Total ~1.5-2% del bet** en costo de entrar

### 2.2 Binance Perp (USDM Futures)

**Fees** (VIP 0, sin BNB discount):
- Maker: 0.020%
- Taker: 0.050%

Si usamos MARKET order = taker. Si LIMIT post-only en best bid/ask → maker, pero riesgo de no fillearse en ventana de 60s pre-close.

**Decisión**: usar MARKET (taker) para garantizar fill atómico.

**Roundtrip perp**: 2 × 0.050% = **0.10% del notional**.

**Funding rate**:
- Pagado/cobrado cada 8h (00:00, 08:00, 16:00 UTC).
- Solo aplica si tenés posición abierta en el snapshot.
- BTCUSDT histórico: -0.03% a +0.03% por 8h. Promedio ~0.01%/8h longs pagan.
- **Nuestra ventana**: bucket de 5min, máx hold ~6min. Probabilidad de cruzar funding snapshot ≈ 6/(8*60) = **1.25%**. Esperado ~0.0001% → **negligible**.

**Spread perp BTCUSDT**:
- En mercado normal: 0.5-1 tick = **~0.5-1 bp** (BTC @ $95k, tick=$0.10 → 1 bp = $9.5).
- En MARKET order tomamos el spread completo → ~1 bp = **0.01%**.

**Margen**:
- Initial margin rate BTCUSDT @ 125x leverage = **0.8%** del notional.
- Para shortear $5 de BTC notional → margin requerido = $0.04. Negligible.
- En la práctica usaríamos leverage 5-10x → margin 10-20% notional → $0.50-$1 por trade.

**Min notional Binance Futures**:
- BTCUSDT minNotional = **$100** USDT 🚨 → **NO podemos hedgear $5**.
- ETHUSDT minNotional = **$20** USDT 🚨
- SOLUSDT/XRPUSDT/BNBUSDT/DOGEUSDT: típicamente $5-$10 (hay que verificar via `/fapi/v1/exchangeInfo`).

⚠️ **PROBLEMA GRAVE**: si nuestro bet Polymarket es $5 y minNotional perp = $100, NO PODEMOS HEDGEAR EXACTO. Opciones:
1. Hedgear OVER (perp $100 vs $5 Polymarket) → no es delta-neutral, es 95% short BTC. NO sirve.
2. Subir bet Polymarket a $100+ → orderbook updown-5m no aguanta, slippage 5-10%. NO sirve.
3. Hedgear solo el delta_BTC equivalente al risk Polymarket → ver §2.3.
4. Usar símbolos con minNotional bajo (DOGE, XRP) → menos liquidez en updown-5m, validar.

### 2.3 Sizing del hedge — el truco

El payoff Polymarket NO es lineal en spot. Es **binario**:
- Si BUY UP @ $5 mid 0.55 → si gana paga $5/0.55 = $9.09. Si pierde paga $0. Edge expected = `bet * (p_real - mid) / mid`.
- Delta vs BTC NO es 1:1. Es `delta = ∂P/∂spot`.

**Para una opción binary cerca del strike (mid 0.50)**:
- Cerca del expiry, delta ~ `bet_usdc / sigma_remaining_pct / spot_price`.
- Ejemplo: bet $5 en BTCUSDT @ $95k, mid=0.55, secs_left=120, sigma=0.05%/min.
  - sigma_remaining = 0.05 × √(120/60) = 0.0707%.
  - delta_USDC ≈ `bet / (sigma_remaining × p × (1-p) × √(2π))` (gauss density at strike).
  - delta_BTC = delta_USDC / spot.

⚠️ El delta de un binario cerca del expiry es **EXTREMO** (gamma alta). Para $5 bet a 2min del close puede pedir hedgear $50-200 notional BTC.

**Fórmula práctica**:
```python
# delta_usd = sensibilidad de PnL Polymarket a un move +1% en spot
# Aprox via numerical: re-compute p_up con spot_move + 1%, ver cuánto cambia bet*p_up/mid
def hedge_notional_btc(bet_usdc, mid, spot_move_pct, secs_left, sigma):
    p0 = implied_up_probability(spot_move_pct, secs_left, sigma)
    p1 = implied_up_probability(spot_move_pct + 1.0, secs_left, sigma)
    payoff_now  = bet_usdc * p0 / mid          # expected payout actual
    payoff_up1  = bet_usdc * p1 / mid          # si spot sube 1%
    delta_usdc_per_1pct = payoff_up1 - payoff_now
    # Si compramos UP, ganamos plata si spot sube → para neutralizar, SHORT BTC.
    # Notional BTC tal que +1% spot = -delta_usdc_per_1pct PnL hedge
    notional_btc = delta_usdc_per_1pct * 100   # 1% = 0.01 → notional = delta/0.01
    return notional_btc                         # USDT, lo convertís a quantity con /spot
```

**Estimación**: para bet $5, 2min restantes, sigma 0.05%/min:
- sigma_remaining = 0.0707%
- p_up @ move 0% = 0.500
- p_up @ move 1% = ~0.999 (porque 1% >> 0.07% de sigma)
- delta_usdc_per_1pct ≈ `5 * (0.999 - 0.500) / 0.55` ≈ **$4.54** por 1% move
- Notional_perp ≈ $4.54 / 0.01 = **$454** 🚨

→ **LA CUENTA NO CIERRA**: hedgear $5 bet Polymarket cerca del expiry requiere ~$450 notional perp BTC. Costos del hedge: $450 × 0.10% = **$0.45 = 9% del bet**.

→ **Si hedgeamos LEJOS del expiry** (5min restantes, sigma_remaining = 0.05% × √5 = 0.112%):
- delta_usdc_per_1pct más razonable ≈ $1-2 → notional perp ~$100-200.
- Costos: $0.10-0.20 = 2-4% bet.

→ **CONCLUSIÓN BRUTAL**: el hedge solo tiene sentido **temprano en el bucket** (≥3min antes del close). Cerca del expiry el gamma explota y el hedge es prohibitivamente caro.

### 2.4 Total cost roundtrip — escenario realista

**Escenario**: bet $5 Polymarket UP @ mid 0.55, 4 min antes del close, BTCUSDT.

| Concepto                          | Costo USDC  | % bet  |
|-----------------------------------|-------------|--------|
| Polymarket fee (entry, taker)     | $0.045      | 0.90%  |
| Polymarket slippage (thin book)   | $0.025      | 0.50%  |
| Polymarket settle (gratis)        | $0          | 0%     |
| Perp Binance entry (taker $200)   | $0.10       | 2.00%  |
| Perp Binance exit (taker $200)    | $0.10       | 2.00%  |
| Perp spread roundtrip (~1bp)      | $0.02       | 0.40%  |
| Funding 8h (negligible)           | $0          | 0%     |
| **TOTAL**                         | **$0.29**   | **5.8%** |

→ Necesitamos un **edge esperado ≥ $0.29** = 5.8% del bet para break-even.
→ Edge expected = `bet × (p_real - mid)` aproximadamente.
→ `5 × (p_real - 0.55) ≥ 0.29` → **p_real ≥ 0.608** = 5.8pp por encima de mid.

**Comparación**: el modelo actual `edge_vs_mid` skipea si edge < `MIN_EDGE` (default 0.10 = 10pp). Si subimos `MIN_EDGE` a 0.06 minimum (para ser rentables hedgeados), el rate de trades baja drásticamente.

**Sin hedge** (estrategia actual paper):
- Cost: $0.07 = 1.4% bet → necesita p_real ≥ 0.564 → edge ≥ 1.4pp.
- Pero risk = bet completo si pierde la dirección.

**Tradeoff**:
- Sin hedge: alta varianza, edge requerido bajo (~1.4pp), pero ~50% prob de perder $5 entero.
- Con hedge: baja varianza (capturás solo el edge), edge requerido más alto (~5.8pp), perdés solo costos si dirección cambia.

→ **Hedged es Sharpe-positivo si el modelo tiene edge calibrado en >5-6pp consistente**. Sharp dice: matemática neutral, ganás solo el spread, no la dirección.

---

## 3. Pseudo code clave

### 3.1 `compute_hedge_edge`

```python
def compute_hedge_edge(
    spot_move_pct: float,
    secs_left: float,
    mid_polymarket: float,
    bet_usdc: float,
    symbol: str,
    spot_price: float,
) -> dict | None:
    """Devuelve edge neto USDC y sizing del hedge perp.

    Returns:
        {
            "edge_gross_usdc": float,    # edge teórico antes de costos
            "edge_net_usdc": float,       # edge - costos hedge
            "side": "Up" | "Down",
            "perp_side": "SHORT" | "LONG",
            "perp_notional_usdc": float,
            "perp_quantity": float,       # ya redondeado a stepSize
            "p_up_model": float,
            "costs_breakdown": dict,
        }
        o None si edge_net <= 0 o min_notional violado.
    """
    sigma = get_sigma_pct_per_min(symbol)
    p_up = implied_up_probability(spot_move_pct, secs_left, sigma)

    # Decisión side (reusa lógica existente)
    if p_up > mid_polymarket + 0.01:        # threshold mínimo BAJO (1pp)
        side = "Up"
        edge_pp = p_up - mid_polymarket
        outcome_idx = 0
        entry_price = mid_polymarket
    elif p_up < mid_polymarket - 0.01:
        side = "Down"
        edge_pp = mid_polymarket - p_up
        outcome_idx = 1
        entry_price = 1.0 - mid_polymarket
    else:
        return None

    # Edge gross USDC: bet_usdc * edge_pp / entry_price
    # (porque comprás bet/entry shares y cada share gana edge_pp si modelo acierta)
    edge_gross_usdc = bet_usdc * edge_pp / entry_price

    # Sizing perp: delta numérico
    p_up_plus  = implied_up_probability(spot_move_pct + 1.0, secs_left, sigma)
    p_up_minus = implied_up_probability(spot_move_pct - 1.0, secs_left, sigma)
    # delta de payoff por +1% spot move
    if side == "Up":
        delta_per_pct = bet_usdc * (p_up_plus - p_up) / entry_price
        perp_side = "SHORT"  # spot sube → Polymarket UP gana → necesitamos perp pierda → SHORT
    else:
        # comprando DOWN: spot baja → Polymarket DOWN gana → necesitamos LONG
        delta_per_pct = bet_usdc * (p_up_minus - p_up) / entry_price * -1
        perp_side = "LONG"
    # Notional para neutralizar 1% move
    perp_notional_usdc = abs(delta_per_pct) * 100  # 1% = 1 unidad de delta_per_pct

    # Min notional check
    MIN_NOTIONAL = {
        "BTCUSDT": 100, "ETHUSDT": 20, "SOLUSDT": 5,
        "XRPUSDT": 5, "BNBUSDT": 5, "DOGEUSDT": 5,
    }
    if perp_notional_usdc < MIN_NOTIONAL.get(symbol, 100):
        # Bumpear a min_notional → over-hedge → no es delta-neutral
        # Decisión: si min_notional / perp_notional_ideal > 2x, SKIP.
        ratio = MIN_NOTIONAL[symbol] / max(perp_notional_usdc, 0.01)
        if ratio > 2.0:
            return None  # over-hedge sería >50% directional risk
        perp_notional_usdc = MIN_NOTIONAL[symbol]

    # Quantity perp (BTC, ETH, etc) — redondeo a stepSize via exchangeInfo
    perp_quantity = round_to_step(perp_notional_usdc / spot_price, symbol)

    # Costos
    fee_pmkt = bet_usdc * 0.018 * (entry_price * (1 - entry_price))
    fee_perp = perp_notional_usdc * 0.001        # 0.05% × 2 (taker entry+exit)
    spread_perp = perp_notional_usdc * 0.0001    # ~1bp roundtrip
    total_cost = fee_pmkt + fee_perp + spread_perp

    edge_net = edge_gross_usdc - total_cost
    if edge_net <= 0:
        return None

    return {
        "edge_gross_usdc": edge_gross_usdc,
        "edge_net_usdc": edge_net,
        "side": side,
        "outcome_index": outcome_idx,
        "perp_side": perp_side,
        "perp_notional_usdc": perp_notional_usdc,
        "perp_quantity": perp_quantity,
        "p_up_model": p_up,
        "costs_breakdown": {
            "pmkt_fee": fee_pmkt,
            "perp_fee_rt": fee_perp,
            "perp_spread_rt": spread_perp,
            "total": total_cost,
        },
    }
```

### 3.2 Orchestrator atómico

```python
async def open_hedged_trade(market, decision, perp_client, db_conn):
    """Abre Polymarket BUY + perp short. Atomicidad best-effort.

    Strategy:
        1. Pre-check perp account: balance, position mode = HEDGE.
        2. Compute idempotency keys.
        3. Open Polymarket FIRST (más lento, más probable que falle).
        4. Si Polymarket OK → open perp con timeout 5s.
        5. Si perp FAIL → ROLLBACK: cerrar Polymarket vía SELL @ market.
        6. Record DB: tabla hedge_pairs con (pid_pmkt, ord_id_perp, status).

    Si rollback FAIL (ambos bookings) → status='unhedged_open', alert Telegram.
    """
    # 1. Pre-checks
    balance = await perp_client.get_balance("USDT")
    if balance < decision["perp_notional_usdc"] * 0.20:  # margin 20% safety
        log.warning("hedge: insufficient perp balance, skipping")
        return None

    # 2. Idempotency
    client_order_id = f"hedge-{market['slug']}-{int(time.time())}"

    # 3. Open Polymarket
    pid = await asyncio.to_thread(open_position, ...)  # existing
    if not pid:
        return None

    # 4. Open perp (5s budget)
    try:
        perp_resp = await asyncio.wait_for(
            perp_client.market_order(
                symbol=market["symbol"],
                side="SELL" if decision["perp_side"] == "SHORT" else "BUY",
                position_side=decision["perp_side"],
                quantity=decision["perp_quantity"],
                client_order_id=client_order_id,
            ),
            timeout=5.0,
        )
    except (asyncio.TimeoutError, Exception) as e:
        # 5. ROLLBACK
        log.error("hedge: perp failed, rolling back Polymarket pid=%d err=%s", pid, e)
        try:
            await rollback_polymarket(pid)
            return None
        except Exception:
            log.exception("hedge: ROLLBACK FAILED — pid=%d UNHEDGED", pid)
            await alert_telegram(f"UNHEDGED open: pid={pid}, manual close needed")
            db_conn.execute(
                "UPDATE paper_trades SET status='unhedged_open' WHERE id=?", (pid,))
            return pid  # dejar abierto, gestionarse manual

    # 6. Record hedge_pair
    db_conn.execute("""
        INSERT INTO hedge_pairs (pmkt_pid, perp_order_id, perp_symbol,
            perp_side, perp_quantity, perp_entry_price, status, opened_at)
        VALUES (?, ?, ?, ?, ?, ?, 'open', strftime('%s','now'))
    """, (pid, perp_resp["orderId"], market["symbol"],
          decision["perp_side"], decision["perp_quantity"],
          float(perp_resp["avgPrice"])))
    return pid
```

### 3.3 Settler

```python
async def settle_hedge_pair(pair, polymarket_client, perp_client):
    """Cuando bucket Polymarket resolvió → cerrar perp con MARKET reduceOnly."""
    # 1. Verificar que Polymarket settled
    if pair["pmkt_status"] not in ("settled_win", "settled_loss"):
        return  # esperá

    # 2. Close perp leg con reduceOnly
    close_side = "BUY" if pair["perp_side"] == "SHORT" else "SELL"
    close_resp = await perp_client.market_order(
        symbol=pair["perp_symbol"],
        side=close_side,
        position_side=pair["perp_side"],  # mismo side label, reduceOnly cierra
        quantity=pair["perp_quantity"],
        reduce_only=True,
        client_order_id=f"close-{pair['id']}",
    )

    perp_pnl = (
        (float(close_resp["avgPrice"]) - pair["perp_entry_price"])
        * pair["perp_quantity"]
        * (1 if pair["perp_side"] == "LONG" else -1)
    )
    perp_fees = pair["perp_quantity"] * (
        pair["perp_entry_price"] + float(close_resp["avgPrice"])
    ) * 0.0005

    total_pnl = pair["pmkt_pnl"] + perp_pnl - perp_fees
    db_update_pair(pair["id"], status="settled", perp_pnl=perp_pnl, total_pnl=total_pnl)
```

---

## 4. Riesgos + mitigaciones

### 4.1 Pierna falla (one-leg execution)

| Caso                               | Impacto                       | Mitigación                                   |
|------------------------------------|-------------------------------|----------------------------------------------|
| Polymarket FAIL antes de perp      | $0 — perp no abrió            | No-op, log skip                              |
| Polymarket OK, perp FAIL           | Position naked en Polymarket  | Rollback Polymarket inmediato (SELL @ mkt)   |
| Polymarket OK, perp OK, rollback FAIL | UNHEDGED open               | Alert Telegram, status='unhedged_open', manual |
| Polymarket settles, perp queda     | Naked perp                    | Settler cron cada 30s, retry 5 veces         |
| Polymarket NO settles (gamma stuck)| Naked perp days               | Timeout 1h → close perp, mark pmkt 'orphaned'|

### 4.2 Riesgos de mercado

- **Gamma explosion cerca del expiry**: skip trades con `secs_left < 60s` (perp_notional explota).
- **Funding rate spike**: improbable en hold de 5min, pero edge case si abrimos 30s antes de funding snapshot. Mitigación: si `secs_to_funding < 5min`, skip.
- **Binance liquidación**: si usás cross margin con position mode HEDGE, riesgo bajo (long+short se cancelan). Pero si tenés OTROS shorts BTC abiertos del bot crypto_arb, suman al risk total. Mitigación: subaccount dedicado, cross margin, leverage 3-5x.
- **Spot vs perp basis divergence**: el perp puede irse 0.1-0.5% del spot por funding-driven imbalance. Negligible en 5min, ignorar.
- **Polymarket settlement delay**: ya lo manejamos en `_settle_crypto_arb_resolved`. Para hedge, perp queda abierto hasta que Polymarket cierre.

### 4.3 Riesgos operativos

- **API key compromise**: clave Binance debe ser **futures-only, no withdrawal, IP whitelist**. Almacenar en env var, NUNCA en repo.
- **Rate limits Binance**: 2400/min IP + 300 orders/min. Bot abre máx 12 ops/hora → muy lejos del límite.
- **Reloj desincronizado**: `recvWindow` default 5000ms, tolera 5s de skew. Sync NTP en server.
- **Reentrancy**: si el loop dispara dos veces el mismo trade, el `client_order_id` único previene doble open en Binance.

### 4.4 Riesgos de modelo

- **Sigma incorrecta**: `SYMBOL_VOL_PCT_PER_MIN` está hardcoded. En periodos de high vol (FOMC, halvings), real sigma puede ser 2-3x. Mitigación: rolling window 30min de stdev real, override env var.
- **Drift no-cero**: el modelo asume drift=0. En tendencias fuertes, p_up está sesgado. Mitigación: regression check con 7d de data.
- **Mid manipulation**: orderbooks thin permiten que un solo trader mueva el mid 5pp. Detectar via depth analysis (ya hay un thread en bot existente).

---

## 5. Estimación PnL — realista

### 5.1 Asunciones

- Edge model bien calibrado: edge_real = edge_modeled × 0.7 (factor de discount por slippage + drift no modelado).
- Threshold operativo: `MIN_EDGE_NET = $0.10` (tras costos).
- Bet size: $5 Polymarket → notional hedge $50-200 perp.
- Trades efectivos por hora: depende del filtrado.

### 5.2 Throughput esperado

Con datos del bot existente (ver heartbeats):
- Markets evaluados: ~6 símbolos × 12 buckets/hr = 72 markets/hr.
- Skipped por `low_edge` (default 0.10): ~95%.
- Skipped por `slope_disagrees`: ~2%.
- Skipped por `overbought`: ~1%.
- → **~1-3 trades/hr** posibles.

Si bajamos `MIN_EDGE_NET` a $0.10 (≈ 4-6pp en p para bet $5), y agregamos check de `min_notional` perp:
- Filtro adicional `secs_left ≥ 90s` (gamma manageable): ~30% reducción.
- Filtro `min_notional` skip: ~10% reducción.
- → **~0.5-2 trades/hr** efectivos.

### 5.3 PnL/trade

- Edge expected neto = $0.10-0.30 por trade.
- Win rate (modelo + hedge): ~70% (el hedge captura solo el spread, no la dirección — la varianza colapsa).
- PnL promedio: `0.7 × $0.20 - 0.3 × $0.05` = **$0.13 USDC/trade**.

### 5.4 PnL/hora y por día

- 1.5 trades/hr × $0.13 = **$0.20/hr**.
- 24h × $0.20 = **$4.80/día**.
- 30 días: **~$144/mes**.

→ Con $200 capital total inicial (pmkt + perp), eso es **72%/mes ROI bruto**.

⚠️ **DISCLAIMER**: estos números son optimistas. Realidad puede ser:
- Edge model realmente calibrado para 0.5x el edge teórico → PnL/2.
- Slippage Polymarket 2x peor en momentos volátiles → break-even.
- Funding spikes → -10% PnL en días malos.
- → **Estimación honesta: $1-3/día con $200 capital. Validar 7 días paper antes de live.**

---

## 6. Min capital para arrancar

| Cuenta            | Concepto                                    | Min USDC/USDT |
|-------------------|---------------------------------------------|---------------|
| Polymarket        | Bet rolling, ~3 trades concurrent × $5      | $20           |
| Polymarket        | Buffer slippage + fees                      | $5            |
| Binance Futures   | Margin perp, 10x leverage, max notional $300 concurrent | $50  |
| Binance Futures   | Buffer funding + slippage + safety          | $50           |
| **TOTAL inicial** |                                             | **$125**      |
| **Recomendado**   | Buffer 2x para drawdowns                    | **$250**      |

**Distribución recomendada**:
- Polymarket: $50 (10 trades concurrent buffer).
- Binance Futures: $200 (margen para hedgear $1500 notional roundtrip, leverage 7-8x cross).

---

## 7. Camino dev: 5 días

### Día 1 — Infra Binance (signed client)

- [ ] `src/binance/perp_client.py`:
    - `class BinancePerpClient` con httpx async.
    - `_sign(params)` → HMAC-SHA256 query string.
    - `market_order(symbol, side, position_side, quantity, reduce_only=False, client_order_id=None)`.
    - `get_position(symbol)` → `/fapi/v2/positionRisk`.
    - `get_balance(asset='USDT')` → `/fapi/v2/balance`.
    - `set_position_mode_hedge()` → `/fapi/v1/positionSide/dual` (one-time setup).
    - `get_exchange_info_filters(symbol)` → cachea `stepSize`, `minNotional`, `tickSize`.
    - Tests con `httpx.MockTransport` + responses sample.
- [ ] env vars: `BINANCE_FUTURES_API_KEY`, `BINANCE_FUTURES_API_SECRET`, `BINANCE_FUTURES_TESTNET=true`.
- [ ] CI: smoke test contra **testnet** (`testnet.binancefuture.com`) — abrir/cerrar 1 BTCUSDT $100 notional.

### Día 2 — Edge calc + sizing

- [ ] `src/copybot/crypto_arb_signals.py`:
    - Agregar `compute_hedge_edge(...)` (ver §3.1).
    - Test: sizing perp para casos extremos (gamma explosion cerca expiry).
- [ ] `src/copybot/crypto_arb_hedge.py`:
    - Esqueleto `crypto_arb_hedge_loop()` paralelo a `crypto_arb_loop()`.
    - Reuse `_list_active_updown_markets` y `_evaluate_market` (refactor a helper).
    - `_evaluate_hedged(market, ws, config)` reemplaza `_evaluate_market` con compute_hedge_edge.

### Día 3 — Atomic open + DB

- [ ] DB migration: tabla `hedge_pairs`:
    ```sql
    CREATE TABLE hedge_pairs (
        id INTEGER PRIMARY KEY,
        pmkt_pid INTEGER NOT NULL REFERENCES paper_trades(id),
        perp_order_id TEXT NOT NULL,
        perp_symbol TEXT NOT NULL,
        perp_side TEXT NOT NULL,           -- 'LONG'|'SHORT'
        perp_quantity REAL NOT NULL,
        perp_entry_price REAL,
        perp_exit_price REAL,
        perp_pnl REAL,
        perp_fees REAL,
        status TEXT NOT NULL DEFAULT 'open', -- 'open'|'settled'|'unhedged_open'|'rollback_failed'
        opened_at INTEGER NOT NULL,
        settled_at INTEGER,
        UNIQUE(perp_order_id)
    );
    CREATE INDEX idx_hedge_pairs_status ON hedge_pairs(status);
    CREATE INDEX idx_hedge_pairs_pmkt ON hedge_pairs(pmkt_pid);
    ```
- [ ] `open_hedged_trade(...)` con rollback path.
- [ ] Test: simular perp fail, verificar rollback Polymarket via mock.

### Día 4 — Settler + observabilidad

- [ ] `_settle_hedge_pair(pair, ...)` integrado al ciclo de `_settle_crypto_arb_resolved`.
- [ ] Telegram alerts: `unhedged_open`, `rollback_failed`, daily summary PnL hedged.
- [ ] Métricas en `crypto_arb_metrics.json`:
    - `hedge_pairs_open`, `hedge_pairs_settled`, `hedge_pnl_24h`, `unhedged_count`.
- [ ] `/api/crypto-arb-status` extender con bloque `hedge`.

### Día 5 — Validación + canary

- [ ] **Paper mode**: correr 24-48h sin abrir perp real (perp_client en dry_run=True, log only).
- [ ] Comparar PnL teórico hedge vs PnL real Polymarket — distribuciones deberían divergir si hedge funciona (varianza más baja).
- [ ] Activar **TESTNET** Binance: 24h con $20 testnet, validar: order place, position close, no unhedged orphans.
- [ ] **Canary LIVE**:
    - $50 cuenta perp dedicada (subaccount).
    - `CRYPTO_ARB_HEDGE_ENABLED=true`, `CRYPTO_ARB_BET_SIZE_USDC=2` (mitad del default).
    - 4h supervisión activa.
- [ ] Si OK → producción con bet $5, monitor 7 días.

---

## 8. Decisiones abiertas (para discutir antes de codear)

1. **Polymarket fee real**: validar 1.80% fee en btc-updown-5m con un trade de prueba. La doc dice 1.80% peak (p=0.50). Confirmar exponente y categoría exacta.
2. **¿BTCUSDT min_notional realmente $100?** Verificar via `/fapi/v1/exchangeInfo` en producción. Si bajó a $20 (cambia ocasionalmente), abre el universo a BTC/ETH/SOL todos.
3. **Position mode**: HEDGE permite long+short simultáneo. Si el bot también opera otras estrategias en perp en futuro, conviene HEDGE. Si no, ONE-WAY es más simple.
4. **Subaccount Binance**: separar fondos del trading manual del user. Recomendado por security + accounting.
5. **¿Maker rebate Polymarket vale la pena?** Si posteamos LIMIT en orderbook en lugar de tomar liquidez, ahorramos 80% del fee (rebate 20% redistribuido). Pero LIMIT = no fill garantizado en ventana 60s pre-close. **Riesgo > beneficio para esta estrategia**, mantener taker.
6. **Stop loss perp**: si el perp side se mueve 5% en contra (improbable en 5min, pero posible en flash crash), ¿auto-close o aguantar al settlement Polymarket? Defecto: aguantar (delta-neutral).

---

## 9. Referencias clave

### Binance docs
- `https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info` — auth, signing, base URL.
- `https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api` — POST /fapi/v1/order params.
- `https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Change-Position-Mode` — set hedge mode.
- Library: **binance-connector-python** (oficial, mantenido). Alternativa: `python-binance` (community, más completa pero a veces lag de updates).
- **Recomendación**: usar **httpx custom + manual HMAC** para tener control fino y reducir deps. ~150 líneas de código. Pattern ya existe en `src/binance/websocket.py`.

### Polymarket docs
- `https://docs.polymarket.com/trading/fees` — fórmula fee `C × p × feeRate × (p×(1-p))^exponent`.
- `https://docs.polymarket.com/trading/orderbook` — CLOB structure.
- Crypto category exponent = 1, peak feeRate = 1.80%.

### Existing code refs (no tocar todavía)
- `/Users/rafalez72/polymarket_copybot/src/copybot/crypto_arb.py` — main loop, settler.
- `/Users/rafalez72/polymarket_copybot/src/copybot/crypto_arb_signals.py` — `edge_vs_mid`, `implied_up_probability`.
- `/Users/rafalez72/polymarket_copybot/src/binance/websocket.py` — WS pattern (asyncio + reconnect backoff), reusable para perp WS user data si lo necesitamos en futuro.

---

## 10. Veredicto final

**¿Es viable?** Sí, marginalmente. Edge esperado neto ~$0.10-0.30/trade post-costos.
**¿Es prioritario?** Tier 2. Si la versión sin hedge ya está dando >$2/día consistente, hedgear puede mejorar Sharpe a costa de PnL absoluto.
**¿Riesgo principal?** **Min notional Binance**: si BTCUSDT requiere $100 notional mínimo, hedgear bets de $5-10 fuerza over-hedge → directional risk → estrategia rota. **Mitigación**: priorizar SOL/XRP/BNB/DOGE primero (min_notional $5-10). BTC/ETH solo si bet ≥ $20.
**¿Worth?** Solo si:
1. Modelo `edge_vs_mid` está empíricamente validado con >7 días paper.
2. Capital ≥ $250 dedicado.
3. Operador disponible para babysit primer 24h LIVE (rollback issues).

**Si NO se cumplen → seguir con paper mode sin hedge, gather data, revisitar en 30 días.**

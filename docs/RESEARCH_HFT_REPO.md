# Research HFT Repo + XGBoost Crypto

**Fecha**: 2026-05-10
**Objetivo**: estudiar `eshan-bhimani/polymarket-hft-bot` y comparar con nuestro N2 (`crypto_arb.py`), + scout de XGBoost para crypto direccional.
**Repo target**: <https://github.com/eshan-bhimani/polymarket-hft-bot>
**Tagline upstream**: "Polymarket BTC 15-min prediction market bot — hybrid Python/C++ with RL (PPO + LSTM) and cross-venue arbitrage"

---

## 1. Architecture Overview (Py + C++)

### 1.1 Layout

```
.
├── main.py                  # CLI entry: train | live | backtest
├── agent.py                 # PPO trainer + LSTM Actor-Critic
├── environment.py           # Gymnasium env (8-dim obs, 4 actions)
├── features.py              # ProbabilityEngine (log-normal), Bregman, arb detect
├── polymarket_client.py     # py-clob-client wrapper (limit/FOK)
├── backtest.py              # CSV replay engine
├── config.py                # Hyperparams + trading constants
├── src/                     # C++20 execution engine
│   └── fast_execution.cpp   # WS ingest + L2 OB + EIP-712
├── scripts/                 # Synthetic GBM data gen
├── CMakeLists.txt + Makefile
└── pyproject.toml + requirements.txt
```

**Language split (GitHub linguist)**: Python 70.1%, C++ 25.5%, Makefile 3.2%, CMake 1.2%.

### 1.2 Por qué Py+C++

- **Python = brain**: RL training (PyTorch PPO + LSTM), feature engineering, CLI, backtest. Productividad alta, ecosistema ML maduro.
- **C++ = nerves**: WS ingestion (Boost.Beast TLS sessions), L2 orderbook con `std::shared_mutex` (multi-reader, single-writer), EIP-712 signing (secp256k1 vía OpenSSL). pybind11 expone módulos al Python.
- **Trade-off declarado**: Python-only mode funciona (paper/backtest); C++ es opcional para "live with tight latency". ExecutionEngine corre `boost::asio::io_context` multi-thread.

**Motivación**: el GIL de Python no aguanta WS sub-tick + signing on-chain en hot path. Splittean: Py orquesta, C++ ejecuta. Igual al patrón Hummingbot / nautilus-trader.

### 1.3 Ciclo de datos

```
Binance WS ──┐
Polymarket WS┼─→ C++ OrderBook (shared_mutex) ─→ pybind11 ─→ Python features.py
Kalshi REST ─┘                                                   │
                                                                  ▼
                                                        environment.step(obs)
                                                                  │
                                                                  ▼
                                                          agent.act() → action
                                                                  │
                                                                  ▼
                                                  C++ ExecutionEngine (EIP-712 sign + POST)
```

---

## 2. Edge Model (cómo calcula prob)

### 2.1 Log-Normal probabilidad fair

Modelo Black-Scholes binario:

```
P(S_T > K) = Φ( [ ln(S₀/K) + (μ − ½σ²)·T ] / (σ·√T) )
```

- `S₀` = spot Binance ahora.
- `K` = strike del market Polymarket ("BTC > $100k").
- `T` = tiempo restante (fracción anualizada).
- `σ` = vol anualizada desde **rolling 60s** de log-returns por tick. Escala per-tick a annual scaling factor.
- `μ` = drift = `0.0` por defecto (config.py).

### 2.2 Bregman / KL divergence

```
D_KL(p || q) = p·ln(p/q) + (1-p)·ln((1-p)/(1-q))
```

- `p` = prob log-normal (modelo).
- `q` = midpoint Polymarket.
- Clamp `[1e-8, 1-1e-8]` para evitar singularidad en log.
- Alimenta directo el observation space (idx 1).

### 2.3 Arbitrage detection (`features.check_arbitrage()`)

Cruza tres fuentes:
1. Polymarket "Yes" mid.
2. Kalshi sintético (probabilidad implícita desde su CLOB equivalente).
3. Log-normal fair value.

Trigger: `|edge| > 3 cents` (`config.ARBITRAGE_THRESHOLD`, default 3¢).

**Punto interesante**: combina arbitrage cross-venue **+ modelo fair**, no solo midpoint diff. Si Kalshi y Polymarket coinciden pero log-normal disagrees → todavía dispara.

### 2.4 Indicadores soporte

- EMA-20 (default).
- ATR proxy = `mean(|tick_returns|)` (no usa OHLC).

---

## 3. Order Execution Strategy

### 3.1 Tipos soportados (`polymarket_client.py`)

| Tipo | Args | Uso |
|------|------|-----|
| **Limit** | `LimitOrderArgs(price, size, expires=10min)` | Action 3 = "limit at midpoint" (50% fill prob asumido en sim) |
| **Market FOK** | `MarketOrderArgs(size, worst_price)` | Actions 1/2 = aggressive cross |

`worst_price` = guardrail slippage:
- BUY: max acceptable price.
- SELL: min acceptable price.

**Caller calcula el `worst_price` antes** — el wrapper no hace cálculo de slippage automático.

### 3.2 Thin orderbook

**No hay manejo explícito**. El código:
- Expone `get_orderbook()` y `get_spread()` (raw L2).
- No tiene retry, circuit breaker, partial-fill handling más allá del que da `py-clob-client`.
- Comentario en código: "for real-time tracking, use the OrderManager in the C++ engine" → admite que `net_position()` Python es best-effort.

**Implicancia**: si OB tiene depth $500 y mandás MarketOrder de $500, te comés todo el book. La defensa única es el `worst_price` pre-computado.

### 3.3 Action space (RL)

```
0 = Hold
1 = Buy YES aggressive  (FOK market)
2 = Buy NO  aggressive  (= sell YES aggressive)
3 = Limit @ midpoint    (passive)
```

Position sizing por trade: `min(remaining_capacity, 0.25 × max_position_usdc)` → sea max **$125 USDC por click** (con default $500 cap).

---

## 4. Risk Management

### 4.1 Lo que SÍ hay

| Mechanism | Valor |
|-----------|-------|
| Max position cap | **$500 USDC** (`MAX_POSITION_USDC`) |
| Position bounds | `position ∈ [−max, +max]`, hard-clip en env |
| Inventory penalty | activa al `elapsed > 720s` (12 min de los 15 totales) |
| Fee deduction | 20 bps en reward |
| Gas deduction | ~150k units × gwei × $0.50 MATIC/USD |
| Arb threshold | 3¢ mínimo edge |

### 4.2 Lo que NO hay (gap crítico)

- **No stop-loss explícito** — ni hard-stop por % loss ni trailing.
- **No kill-switch** global (`global_pause` style).
- **No 12-min forced exit** — el comentario marketing del repo dice "12-minute position holding limit" pero el código solo aplica una **penalty linealmente creciente**, no un exit forzado. Único termination = `elapsed >= 900s` (fin del contrato).
- **No max-drawdown monitor** a nivel episodio.
- **No position-size constraint en agent.py** — el buffer PPO acumula transitions sin validación de catastrophic loss.

Inventory penalty fórmula:
```python
overshoot = (elapsed − 720) / (900 − 720)   # 0..1 entre min 12 y 15
penalty = |position| × 0.01 × min(overshoot, 1.0)
```

→ es un **shaping reward**, no un hard rule. El RL "aprende" a salir, no se le obliga.

### 4.3 PPO hyperparams (config.py)

```
PPO_LR        = 3e-4
PPO_GAMMA     = 0.99
PPO_GAE_LAMBDA= 0.95
PPO_CLIP_EPS  = 0.2
PPO_EPOCHS    = 4
PPO_BATCH     = 64
LSTM_HIDDEN   = 128 (1 layer)
ENTROPY_COEF  = 0.01
GRAD_CLIP     = 0.5
```

---

## 5. Settlement Handling

### 5.1 On-chain redemption

**No hay logic explícito de auto-redeem en el wrapper Python**. El comentario del client dice:
> "EIP-712 order creation and signing (via the private key) handles cryptographic commitment to on-chain settlement without explicit redemption logic in this wrapper."

Lo que sí hace:
- Firma órdenes EIP-712 (open + close).
- POST al CLOB de Polymarket vía `create_and_post_order()`.
- Polymarket internamente hace settlement on-chain en Polygon.

### 5.2 Reward / settlement en env

`environment.py` calcula `Realised PnL` cuando:
1. Posición se cierra explícitamente (action 2 vs 1, o limit fill al opuesto).
2. Episode termina (`elapsed >= 900s`) → asume settlement al precio fair correcto (binario 0/1).

**No espera settlement on-chain real** — es modelo simulado durante backtest/train. En live, asume que el CLOB hace match y resuelve solo (cierto para Polymarket: el ConditionalToken se redime via UMA después).

**Gap nuestro vs ellos**: nosotros también dejamos `status='open'` hasta polling de markets activos detecte el cierre. Mismo problema.

---

## 6. Lessons Learned / Warnings

### 6.1 GitHub Issues

**Vacío**. 0 open, 0 closed. Repo personal académico-research, sin community feedback.

### 6.2 README warnings explícitos

> "Trading prediction markets and cryptocurrencies involves substantial risk of loss. The authors are not responsible for any financial losses incurred from using this software."

> "for educational and research purposes only"

### 6.3 Lecciones que se infieren leyendo el código

1. **Settlement no auto-redeem** → como nosotros, dependen de UMA + CLOB para resolver. No hay reclamo proactivo on-chain.
2. **Synthetic GBM training** → entrenan en data sintética porque ticks históricos de Polymarket son ruidosos / poca data en estos markets de 15min. Riesgo: sim-to-real gap.
3. **3¢ arb threshold** = consensus market making para hedgear fees + gas + slippage. Coincide con threshold típico en Polymarket arbs reportado en research académico (Polymarket spread medio ~$0.02-0.05 en BTC mids).
4. **`worst_price` manual** → el caller debe calcular slippage. Si te olvidás, te volás (riesgo en vivo no mitigado por library).
5. **`OrderManager` Python es best-effort** → para HFT real necesitás el C++ side.

---

## 7. Diferencias punto-por-punto vs nuestro N2 (`crypto_arb.py`)

Nuestro N2 está en `src/copybot/crypto_arb.py` (777 líneas) + `crypto_arb_signals.py` (132 líneas).

| Eje | eshan-bhimani/hft-bot | Nuestro N2 |
|-----|-----------------------|------------|
| **Stack** | Py + C++ (pybind11) | Pure Python (asyncio) |
| **Markets target** | BTC 15-min only | BTC/ETH/SOL/XRP/BNB/DOGE 5-min updown |
| **Spot source** | Binance + Kalshi cross-venue | Binance Spot WS único (`BinanceTickerWS`) |
| **Probability model** | Log-Normal Black-Scholes binario | Normal residual-drift: `P(close>start) = Φ(spot_move / (σ·√t_left/60))` |
| **Vol estimation** | Rolling 60s log-returns annualizado | Per-symbol estática (`SYMBOL_VOL_PCT_PER_MIN`) + override env |
| **Cross-venue arb** | Sí (Polymarket + Kalshi + Binance) | No, solo Polymarket vs Binance temporal lag |
| **Edge metric** | Bregman/KL div + log-normal fair | Probability points: `p_up − mid_up` |
| **Edge threshold** | 3¢ (price) | 10pp (probability points) (`CRYPTO_ARB_MIN_EDGE`) |
| **Decision engine** | RL (PPO + LSTM) entrenado | Deterministic rule-based: edge > thr → buy |
| **Action space** | 4 discrete (hold/buy yes/buy no/limit mid) | 2: buy UP / buy DOWN (al mid) |
| **Order type** | FOK Market o Limit@mid | Limit at mid (`tradebook.open_position`) |
| **Slippage guard** | `worst_price` manual pre-trade | `max_mid_target=0.70` (no compra si mid > 0.70) |
| **Position cap** | $500 USDC, 0.25× per click | $5 USDC default por trade (`bet_size_usdc`) |
| **Stop-loss** | None (penalty shaping) | None |
| **Inventory penalty** | Linear 720s→900s | None — apostás y esperás resolución |
| **Time-to-close window** | Toda la episode (15min) | **180s pre-close window** (`pre_close_window_s`) — solo entra cerca del settle |
| **Min bucket age** | n/a | **60s** (necesita momentum data) |
| **Slope confirmation** | n/a | Sí (`skipped_slope_disagrees`) — última 30s slope debe coincidir con side |
| **Live gating** | Solo paper hasta tener engine | `CRYPTO_ARB_ALLOW_LIVE=false` default; comment explícito sobre slippage 50-90% si pasás a live |
| **Settlement** | Asume on-chain auto via UMA | Idem — `status='open'` hasta polling |
| **Persistence/metrics** | Tensorboard, CSV backtest reports | JSON snapshot atómico → `data/crypto_arb_metrics.json` |
| **Failure modes** | Sin retry, FOK puede no fillear | `fail-open`: si Binance WS desconecta, no abre nada |
| **Training data** | Synthetic GBM + replay CSV | None (no ML) |
| **Code size** | ~70% Python, ~25% C++ | 909 LOC Python total |

### 7.1 Lo que ellos hacen mejor

1. **Modelo log-normal con vol rolling** → captura régimen de vol real-time. Nuestro `SYMBOL_VOL_PCT_PER_MIN` está hardcoded.
2. **Cross-venue (Kalshi)** → más fuentes de edge. Nosotros solo lag temporal Polymarket vs Binance.
3. **Slippage guard `worst_price`** explícito por orden.
4. **C++ execution engine** → latency sub-ms. Nosotros somos `httpx` Python = 50-200ms RTT.
5. **RL puede aprender patrones no triviales** (microestructura, momentum no-lineal).

### 7.2 Lo que nosotros hacemos mejor

1. **Multi-symbol** (6 cryptos vs 1).
2. **Pre-close window (180s)** → reduce exposición a regime change largo. Ellos están en la moneda toda la episode.
3. **Min bucket age (60s)** → evitan signal-to-noise en bucket recién abierto.
4. **Slope confirmation** → segunda señal independiente además del edge model.
5. **Live gate explícito** + comment doc sobre el riesgo. Su README solo tiene disclaimer genérico.
6. **Per-symbol vol override env** sin retraining.
7. **Atomic JSON snapshot** para coordinación cross-container (su shared volume pattern).
8. **Categorización de skips granular** (`skipped_no_spot`, `skipped_slope_disagrees`, `skipped_overbought`, etc.) para post-mortem.
9. **Sin RL = sin entrenamiento, sin sim-to-real gap, sin model drift**. Reproducibilidad total.

### 7.3 Recomendaciones para N2 (priorizadas)

1. **[P0] Vol rolling**: reemplazar `SYMBOL_VOL_PCT_PER_MIN` estática por vol realizada de últimos 5-10 min (rolling stdev de Binance ticks). Ya tenés `BinanceTickerWS` corriendo — agregás un buffer `deque(maxlen=600)` y `numpy.std`.
2. **[P0] Worst-price guard**: cuando hagas BUY UP a mid, abortá si midpoint subió >5pp entre eval y submit. Hoy lo único es `max_mid_target`.
3. **[P1] Stop-loss intra-bucket**: si comprás UP a 0.55 y el mid cae <0.30 (con t_left>120s), cerrá. Hoy esperás resolución → -100% si erra.
4. **[P1] Cross-venue Kalshi opcional**: añadir Kalshi como fuente extra para BTC. Aumenta hit rate y filtra falsos positivos.
5. **[P2] Min OB depth check**: antes de submit, verificar que OB tiene `>=2× bet_size_usdc` en el side a comprar. Skip si no.
6. **[P2] Inventory aging penalty**: si secs_to_close <60s y aún no fillearon, cancelá la limit. Hoy queda ahí 10min default.
7. **[P3] Backtest module**: replicar su `backtest.py` con CSV replay de Binance + snapshots Polymarket. Dataset histórico desde nuestro `ws_metrics`.

---

## 8. XGBoost para Crypto Direccional — Research

### 8.1 Por qué XGBoost (vs LSTM o pure rule)

- **Latency**: entrena en segundos vs LSTM minutos por fold (relevante en walk-forward 13k+ fits).
- **Data hambre**: árboles aguantan datasets chicos (2k velas suficientes); LSTM/Transformer necesitan órdenes de magnitud más.
- **Tabular wins**: gradient boosting domina datos tabulares financieros, faster + más accurate que DL para price prediction (validado en múltiples papers 2024-2025).
- **Walk-forward natural**: incremental retraining mata el non-stationarity de crypto.

### 8.2 Features típicos (consenso open-source)

#### Microstructure / orderbook
- `order_book_imbalance` = `(bid_vol − ask_vol) / (bid_vol + ask_vol)` top N levels
- `trade_flow_imbalance` = signed volume neto en ventana
- `spread_bps`
- `micro_price` = `(ask·bid_qty + bid·ask_qty) / (bid_qty + ask_qty)` — VWAP del top
- `depth_at_X_pct` (cantidad disponible a 0.1%, 0.5% del mid)

#### Price/volume derived
- `log_returns` 1m, 5m, 15m
- `volatility` rolling (Garman-Klass, Parkinson, realized) — varias ventanas
- `volume_change`, `volume_momentum`
- `price_momentum` multi-timeframe
- `vwap` desviación

#### Indicators clásicos (importantes según feature importance)
- `RSI14`, `RSI30` (top scores en papers)
- `MACD` + signal
- `MOM30`
- Bollinger position `%B`
- `%K30`, `%K200` (Stochastic)
- EMA 20/50/200 + cross signals

#### Derivatives (si tradeás perp)
- `funding_rate` actual + 24h moving avg
- `open_interest` Δ
- `long_short_ratio` (Binance/Bybit endpoint)
- basis (perp − spot)

#### Sentimiento (opcional, marginal value)
- CryptoBERT embeddings de news
- Google Trends
- Fear & Greed index

### 8.3 Time horizons

| Horizon | Use case | Win rate típico | Notas |
|---------|----------|-----------------|-------|
| 1m | HFT scalping, OB-based | 51-53% | Necesita latency baja, ruido alto, microestructura |
| 5m | "Polymarket updown 5m fit" | 53-56% | **Ideal para nuestro N2** — coincide con el bucket |
| 15m | Swing intraday | 54-57% | Más estable, menos trades |
| 1h | Position trading | 55-60% | Indicators clásicos pesan más |

**Recomendación N2**: predecir `sign(close[t+5min] − close[t])` cuando empieza el bucket Polymarket (epoch start), o bien `sign(close[bucket_end] − price[t_now])` con `secs_left` como feature.

### 8.4 Walk-Forward Validation — best practices

#### Setup canonical (FreqAI / PyQuantLab)

```
train_period      = 14 días  (≈ 20k velas 1m)
backtest_period   = 1-3 días
sliding_step      = 1 día (retrain diario)
include_shifted_candles = 5-10  (lags)
```

#### Reglas

1. **NUNCA k-fold en time series** — leak garantizado.
2. **Retrain cadence** = compromiso latency vs drift. Crypto = retrain diario o 12h.
3. **Threshold tuning post-fit**: train predice prob [0,1], luego optimizás threshold (`> 0.55` BUY, `< 0.45` SELL) sobre validación, no sobre train.
4. **Métricas correctas**: NO accuracy. Sí Sharpe, hit-rate condicional al threshold, profit factor, max drawdown.
5. **Class balance**: SMOTE o `scale_pos_weight` — crypto tiene rachas trending que sesgan classes.
6. **Standardize por ventana** (z-score con stats del train period actual, no del global) — evita leak.

### 8.5 Cuántos datos de training

| Granularidad | Mínimo razonable | Sweet spot | Diminishing returns |
|--------------|------------------|------------|---------------------|
| 1m | 30 días (43k bars) | **3 meses** | >6m introduce regime change |
| 5m | 3 meses (26k bars) | **6 meses** | >1y |
| 1h | 6 meses (4.3k bars) | **1-2 años** | >3y |

**Trade-off clave**: más data = más patrones genéricos PERO crypto tiene regime shifts (bull/bear/chop). Window deslizante de 3-6m con retraining frecuente > full history.

**Para N2 (5min direccional)**: 3 meses de Binance 1m + features derivados = ~130k bars. Suficiente para XGBoost sin overfit con `max_depth=4-6, n_estimators=200-500, eta=0.05`.

### 8.6 Repos GitHub recomendados

| Repo | Tipo | Uso recomendado |
|------|------|-----------------|
| `freqtrade/freqtrade` (FreqAI module) | Production framework | **Reference architecture**: feature engineering, train_period_days, walk-forward built-in. Estudiar `freqai/prediction_models/XGBoost*.py` |
| `Mayurisarda89/Cryptocurrency-Price-Prediction-using-XGBoost` | Educational | Setup mínimo, OHLCV-only, baseline |
| `baileyarzate/crypto_prediction` | Multi-source | Sentimiento + macro + price, ensemble Linear/Ridge/RF/XGBoost |
| `AaronFlore/Forecasting-Bitcoin-Prices` | Comparative | XGBoost vs ARIMA vs Prophet vs LSTM benchmark |
| `nekrasovp` (gist + blog) | Feature importance | XGBoost como herramienta de feature selection sobre OHLCV+ARIMA+FFT |

**No recomendado**: `lakashk/CryptoCurrency-Price-prediction-Using-Xgboost` (toy), `ent0n29/polybot` (Polymarket-focused pero no ML).

### 8.7 XGBoost vs LightGBM vs CatBoost

| Lib | Latency train | Latency predict | Accuracy crypto | Notas |
|-----|---------------|-----------------|-----------------|-------|
| **XGBoost** | Medio | Bajo | Alta | Default histórico, hist mode = casi LightGBM speed. Buen ecosystem. |
| **LightGBM** | **Más rápido** | **Más rápido** | Alta (a veces unstable) | Ideal para datasets grandes (>1M rows). HFT real-time. Leaf-wise growth → puede overfit en small data. |
| **CatBoost** | Lento | Medio | **Más estable** | Brilla con categorical features (símbolo, hora del día como cat). Defaults sanos. Mejor AUC consistente. |

**Recomendación pragmática para nuestro N2**:

1. **MVP**: `xgboost` (`hist` tree method, `device=cpu`). Estable, docs ricas, integración sklearn directa.
2. **Si dataset crece >500k filas**: migrar a `lightgbm`.
3. **Si features tienen muchas categorical** (símbolo, weekday, sesión asia/europa/us): `catboost` ahorra encoding.
4. **Bench 2025 fraud-detection** (proxy real-time): LightGBM gana latency, CatBoost gana estabilidad. XGBoost el balance.

### 8.8 Pipeline propuesto para N2 — XGBoost layer

```python
# Stage 1: feature builder
features = {
    "spot_move_pct": (price_now - price_bucket_start) / price_bucket_start * 100,
    "secs_left": bucket_end - now,
    "vol_realized_5m": np.std(log_returns_last_5min),  # rolling
    "vol_realized_30m": np.std(log_returns_last_30min),
    "ob_imbalance_top5": ...,                          # if WS L2 disponible
    "rsi_14": ...,
    "ema_fast_slow_ratio": ema20 / ema50,
    "funding_rate": ...,                                # Binance perp
    "vol_24h_change": ...,
    "polymarket_mid_up": mid_up,
    "polymarket_spread": ask - bid,
    "secs_since_bucket_start": ...,
}

# Stage 2: target = sign(close - bucket_start) at bucket_end (binario)
# Stage 3: walk-forward train cada 24h sobre últimos 90d
# Stage 4: predict prob → si prob > 0.58 BUY UP, prob < 0.42 BUY DOWN
# Stage 5: combinar con edge_vs_mid existente como AND-gate
#         (solo entra si AMBOS coinciden → reduce false positives)
```

**XGBoost params iniciales**:
```python
XGBClassifier(
    n_estimators=300,
    max_depth=5,
    learning_rate=0.05,
    subsample=0.8,
    colsample_bytree=0.8,
    objective="binary:logistic",
    tree_method="hist",
    eval_metric="logloss",
    early_stopping_rounds=20,
)
```

### 8.9 Riesgos / contras de meter XGBoost ahora

1. **Sin label data histórica labeleada**: hay que loggear N=10k+ buckets resueltos antes de tener train set serio. Hoy el N2 está en paper, recién empieza a generar data.
2. **Sim-to-real gap**: lo que entrena en data offline ≠ lo que ve live (slippage, OB depth real). Mismo problema que el repo ref.
3. **Maintenance overhead**: retrain pipeline + feature store + model versioning = nuevo subsistema.
4. **Overfit a régimen actual**: si entrenás en bull 2025-2026, el modelo falla en chop o bear.
5. **Rule-based actual ya funciona** (en teoría) sin label data. Antes de ML, **validar 30d el N2 actual** es prioridad.

### 8.10 Recomendación final

**No agregar XGBoost ahora** (Q2-2026). Prioridad:

1. Validar N2 actual en paper 30 días → tener distribución de PnL real.
2. Loggear todas las decisiones + features computados (`crypto_arb_signals.py` ya da edge/p_up/sigma — agregar OB imbalance, vol realizada al log).
3. Una vez con dataset, entrenar XGBoost **como filtro adicional**, no reemplazo. Pipeline AND-gate: rule-based + XGBoost ambos verdes → trade.
4. Si XGBoost mejora hit-rate >3pp consistentemente en backtest walk-forward, deployar como filter. Si no, ditch.

---

## 9. Sources

### Repo target
- <https://github.com/eshan-bhimani/polymarket-hft-bot>

### XGBoost crypto
- <https://pyquantlab.medium.com/xgboost-for-short-term-bitcoin-prediction-walk-forward-analysis-and-thresholded-performance-b83dc2e677eb>
- <https://www.freqtrade.io/en/stable/freqai/>
- <https://www.freqtrade.io/en/stable/freqai-feature-engineering/>
- <https://github.com/freqtrade/freqtrade>
- <https://emergentmethods.medium.com/real-time-head-to-head-adaptive-modeling-of-financial-market-data-using-xgboost-and-catboost-995a115a7495>
- <https://arxiv.org/html/2407.11786v1> — XGBoost regressor + technical indicators for crypto
- <https://arxiv.org/html/2410.06935v1> — Bitcoin trend prediction with technical indicators
- <https://arxiv.org/html/2506.22055v1> — LSTM+XGBoost hybrid
- <https://link.springer.com/article/10.1007/s10614-025-10919-y> — Data types effect on ML for crypto
- <https://xgboosting.com/xgboost-evaluate-model-for-time-series-using-walk-forward-validation/>
- <https://blog.quantinsti.com/walk-forward-optimization-python-xgboost-stock-prediction/>
- <https://github.com/Mayurisarda89/Cryptocurrency-Price-Prediction-using-XGBoost>
- <https://github.com/baileyarzate/crypto_prediction>
- <https://github.com/AaronFlore/Forecasting-Bitcoin-Prices>
- <https://nekrasovp.github.io/feature-engineering-on-ohlcv-candles-wit-xgboost.html>
- <https://dev.to/nydartrading/why-we-chose-xgboost-over-lstm-for-crypto-prediction-487j>
- <https://neptune.ai/blog/when-to-choose-catboost-over-xgboost-or-lightgbm>

### Files internos referenced
- `/Users/rafalez72/polymarket_copybot/src/copybot/crypto_arb.py`
- `/Users/rafalez72/polymarket_copybot/src/copybot/crypto_arb_signals.py`
- `/Users/rafalez72/polymarket_copybot/docs/PROJECT.md` (contexto N2)
- `/Users/rafalez72/polymarket_copybot/docs/RESEARCH_BOTS_2026-05-10.md` (research previo)

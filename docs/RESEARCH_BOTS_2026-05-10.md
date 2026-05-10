# Research bots trading 2026-05-10

> Modo caveman. Output denso. Cero floritura.

## TL;DR

- 84.1% wallets Polymarket pierden plata. Bots se llevan ~$40M/año extrayendo a humanos. Nuestro -$76 no es bug, es regla.
- Copy trading puro = trampa. Top wallets corren **decoy trades**, multi-wallet, **iceberg entries** y usan copiers como exit liquidity. ~15% wallets hacen wash. Si tu wallet "smart money" se hizo popular, ya está quemada.
- Bots ganadores NO predicen, **ejecutan**: latency arbitrage Binance/Coinbase vs Polymarket (ventana 30s-5min noticia, 2.7s arb-pure), market making spread, "bet always NO" (73% mercados resuelven NO).
- Nuestro gap dry-run vs live = **conocido y documentado**. Backtest sin tick data + sin slippage real + sin orderbook depth check = pérdida garantizada en live. Solución: tick replay + spread filter + depth precheck antes de cada market order.
- Bankroll mínimo serio: depende de Kelly fraccional. Full Kelly = 50-80% drawdowns esperados. Industria usa 10-25% Kelly. Si edge calculado < 0.5-1% bankroll por trade, no vale la fricción.

---

## Hallazgos críticos

### 1. Polymarket es campo minado para retail

- 100k+ cuentas perdieron >=$1k desde inicio 2025. Pérdida agregada $131M ([fa-mag](https://www.fa-mag.com/news/most-prediction-market-traders-are-losing-money-while-bots-rack-up-gains-86783.html)).
- Bots arb extrajeron ~$40M/año explotando ineficiencias estructurales ([Yahoo Finance / Bitget](https://finance.yahoo.com/news/arbitrage-bots-dominate-polymarket-millions-100000888.html)).
- Ventana arbitrage promedio cayó de 12.3s (2024) a **2.7s** (2026). 73% de profit arb capturado por bots <100ms latency ([financemagnates](https://www.financemagnates.com/trending/prediction-markets-are-turning-into-a-bot-playground/)).

### 2. Anti-copy tactics confirmados (validan nuestro caso)

- **Decoy / bait-and-switch**: smart money construye track record en wallet A, cuando suficientes copyean migran a B y toman side opuesto, copiers son exit liquidity ([medium 0xmega](https://medium.com/@0xmega/how-to-find-the-best-polymarket-wallets-to-copy-trade-without-getting-rekt-26dd65123324)).
- **Iceberg entries**: pequeñas órdenes piecemeal evitan triggers de copy bots con threshold de volumen.
- **Spread-capture wallets** (HFT) lucen perfectos en leaderboard pero son **NO copyables**: ya cerraron el spread; copy = comprar a market después de que bot capturó (`r/algotrading` patterns + [panewslab](https://www.panewslab.com/en/articles/019d3235-40a0-764d-ab19-5a1d53ed9303)).
- **Wash trading**: ~15% wallets con patrones de wash. Track records "impresionantes" pueden ser sintéticos.
- Mercados exóticos low-liq + copy = estás siendo exit liquidity automática.
- Diciembre 2025: malware en bot popular GitHub robaba private keys. Enero 2026 "ClawdBot" typo-squat package. **Cuidado dependencias.**

### 3. Backtest vs live — gap es estructural, no bug

- "Tu backtest asume sos invisible. El orderbook prueba que no" ([Kalena](https://blog.kalena.ai/crypto-algo-trading-reddit-the-order-flow-audit-stress-testing-the-7-most-upvoted-algorithmic-strategies-against-real-market-microstructure)).
- Estrategia +3% mensual en 1m candles típicamente se vuelve -1% al sumar 0.05-0.15% spread + partial fills + market impact.
- **3 reglas mínimas pre-deploy**: (1) slippage modelado >= 0.05% per side, (2) orderbook depth precheck, (3) kill switch.
- Reemplazar OHLCV por **tick data**, reconstruir candles desde trades para saber qué lado del book absorbió.
- **Spread filter**: skip signals si spread > 1.5x rolling 20m median.
- **Depth check**: antes de market order, verificar liquidez resting dentro de 0.1% precio absorba sin mover >0.03%.

### 4. Lookahead / data leakage / survivorship

- Bias clásicos. Aplican a nuestro caso si re-entrenamos selector de wallets.
- **Time-based splits estrictos** (no random k-fold).
- Scalers/transforms fit solo en train window, aplicar a val/test.
- Hold-out final intocable como auditoría.
- Walk-forward analysis sobre retrain (Lopez de Prado: deflated Sharpe ratio para corregir selection bias ([SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551), [Wikipedia](https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio))).

### 5. MEV / adversarial liquidity en prediction markets

- Polymarket es Polygon → mempool visible → posible frontrunning de market orders ([bitquery](https://bitquery.io/blog/different-mev-attacks)).
- Mitigación: batch auctions o limit orders con tiempo de vida corto. **No usar market orders en thin books**, siempre limit con max-slippage explícito.
- Insider asks: prediction markets vulnerables a info asymmetry (políticos, regulatorios, deportivos con info privilegiada).

### 6. Hedging Polymarket × Binance perps

- Polymarket lanzó perps abril 2026 ([CNBC](https://www.cnbc.com/2026/04/21/polymarket-launches-trading-of-heavily-leveraged-perps-contracts.html)). Mismo venue → menor basis risk vs Binance, pero fees + funding aún relevantes.
- Estrategia ya implementada en bot Nivel 2 (commit b2b3ce5) tiene tracción: latency arb Binance vs Polymarket es **una de las 3 estrategias rentables** confirmadas en literatura ([QuantVPS](https://www.quantvps.com/blog/polymarket-hft-traders-use-ai-arbitrage-mispricing)).
- Caso bot $313 → $414k mes, BTC/ETH/SOL 15-min markets, 98% WR — si es real, es esencialmente nuestra hipótesis de Nivel 2 bien ejecutada.

### 7. Position sizing / Kelly / kill switch

- Full Kelly = drawdowns 50-80% esperados. **Half Kelly** consigue ~75% growth con la mitad de drawdown.
- Profesionales: 10-25% de full Kelly.
- Recovery asimétrico: -10% requiere +11.1%, -20% requiere +25%, -50% requiere +100%. **Evitar drawdown >>> maximizar upside**.
- Circuit breakers obligatorios: daily loss cap, consecutive loss cap, drawdown cap.
- **Si edge < 0.5-1% bankroll por trade, no operar**.

### 8. ML approaches — XGBoost gana en accuracy direccional

- XGBoost 81.1% directional accuracy vs LSTM 48.8% en estudios crypto ([arxiv 2506.22055](https://arxiv.org/abs/2506.22055)).
- Hybrid LSTM+XGBoost o Transformer+XGBoost outperforman individuales pero **costo computacional** alto para tiempo real.
- Para nuestro caso (decisiones segundos, no ms): XGBoost solo + features de sentiment/on-chain es ratio costo/beneficio óptimo.

### 9. Sentiment / on-chain signals

- Whale Alert + Twitter combinados mejoran predicción volatilidad BTC ([VoiceOfChain](https://voiceofchain.com/academy/whale-alerts-twitter)).
- Pero **señal/ruido es problema serio**: muchos movimientos whale son entre wallets propias, no señal de mercado.
- Confirmar siempre con on-chain (exchange inflow/outflow) + price action antes de actuar.

### 10. CLV / closing line value (sports betting wisdom)

- Métrica oro de sharps: ¿tu odd al entrar > odd cierre? Si sí, edge real.
- Aplicable a Polymarket: trackear precio entrada vs precio resolución/cierre. Si consistentemente "beat closing line", tenés edge real más allá de PnL ruidoso.
- Sumar CLV como métrica de monitoreo continuo (early drift detection).

---

## Estrategias prometedoras para nuestro caso

### A. Latency arb Binance ↔ Polymarket crypto markets (PRIORIDAD ALTA)
Ya pivotaste a esto (Nivel 2 commit b2b3ce5). Es **la** estrategia validada por múltiples fuentes. Ventana 30s-5min en news events, sub-segundo en price moves. Necesitás: VPS cerca de nodo Polygon, WS Binance feeds estables, ack <500ms del CLOB.

### B. Market making liquidity provider en mercados >$30k liq
Spread capture: place bid/ask con tight spread, ganar fees + spread. WR reportado 78-85%, returns 1-3% mensual con baja vol ([flypix](https://flypix.ai/openclaw-polymarket-trading/)). Requiere capital quieto + risk de inventory en eventos resolutivos.

### C. "Always NO" baseline para markets non-sports >X días resolución
73% mercados resuelven NO ([decrypt](https://decrypt.co/364381/polymarket-bot-bets-no-has-a-point)). Como **hedge sanity check**: si tu modelo dice YES con prob <60%, considerar skip. Útil como benchmark, no estrategia principal.

### D. News-driven price lag (30s-5min ventana)
News scraper → estimación shift fair value → entrada antes que orderbook ajuste. Combina con (A) cuando news es macro/crypto. Para política/eventos: scraping AP/Reuters/Twitter cuentas verificadas.

### E. Selector wallets multi-señal (no copy ciego)
Reemplazar copy puro por **scoring** que combine: histórico WR + tamaño promedio (filtrar HFT scalpers que ya tenés bucketeados Nivel 1) + holding period + diversidad markets + correlación PnL post-trade. Nunca seguir si wallet trade en market <$30k liq. Hacer **sample/retest** mensual: comportamiento que cambió = wallet quemada.

---

## Mistakes que ya cometimos (validados en research)

1. **Mercados thin (<$30k liq)**: causa #1 documentada de slippage destructivo. Ya pivotaste, correcto.
2. **Dry-run que miente vs live**: gap estructural, no fixable con más logging — necesita tick replay + spread filter + depth check antes de simular fill.
3. **Copy trading sin scoring**: caímos en trampa decoy/wash de top wallets. Necesario clasificar por horizonte (Nivel 1 ya hizo bucket scalper, falta swing-quality filter).
4. **Sin kill switch**: -$76 podría haber sido -$760. Daily loss cap + consecutive loss cap + drawdown cap **obligatorios** antes de seguir paper-to-live.
5. **Sin CLV tracking**: midiendo PnL ruidoso, no edge real. Implementar diff entrada vs cierre.
6. **Position sizing sin Kelly fraccional**: probable over-sized en trades sin edge real.
7. **Market orders en CLOB**: posible MEV / adversarial fill en thin liquidity. Migrar a limit orders con max-slippage.

---

## Recursos open-source útiles

| Repo / Tool | Uso |
|---|---|
| [Polymarket/py-clob-client](https://github.com/Polymarket/py-clob-client) | Cliente oficial Python (ya lo usás) |
| [Polymarket/py-clob-client-v2](https://github.com/Polymarket/py-clob-client-v2) | V2 oficial — chequear migration path |
| [Polymarket/agents](https://github.com/Polymarket/agents/) | Framework AI agents oficial MIT |
| [GiordanoSouza/polymarket-copy-trading-bot](https://github.com/GiordanoSouza/polymarket-copy-trading-bot) | Copy bot Python+Supabase reference |
| [artvandelay/polymarket-agents](https://github.com/artvandelay/polymarket-agents) | MCP server 10 tools (orderbook, spread, history) + bot framework |
| [eshan-bhimani/polymarket-hft-bot](https://github.com/eshan-bhimani/polymarket-hft-bot) | BTC 15-min hybrid Py/C++ con PPO+LSTM y cross-venue arb — **directamente relevante** |
| [aarora4/Awesome-Prediction-Market-Tools](https://github.com/aarora4/Awesome-Prediction-Market-Tools) | Curated list |
| [harish-garg/Awesome-Polymarket-Tools](https://github.com/harish-garg/Awesome-Polymarket-Tools) | Curated list traders/devs |
| [yllvar/Kalshi-Quant-TeleBot](https://github.com/yllvar/Kalshi-Quant-TeleBot) | Quant bot Kalshi (concepts portables a Polymarket) |
| [dev-protocol/polymarket-arbitrage-bot](https://github.com/dev-protocol/polymarket-arbitrage-bot) | Dump-hedge arb strategy |
| [ent0n29/polybot](https://github.com/ent0n29/polybot) | Reverse-engineer strategies — ver para detectar patterns |
| Freqtrade [lookahead-analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/) | Tool detección lookahead bias en backtest |
| Whale Alert API | Señales on-chain whale moves |

**Cuidado seguridad**: malware en GitHub bots Dic-2025 y typo-squat Ene-2026. Auditar deps con `pip-audit` + `safety` antes de instalar. Nunca copiar secret keys en repos públicos.

---

## Personas / cuentas a seguir

- **Marcos López de Prado** — backtest overfitting, deflated Sharpe ([SSRN papers](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)). Lectura obligatoria.
- **David H. Bailey** — co-autor backtest probability papers ([davidhbailey.com](https://www.davidhbailey.com/dhbpapers/)).
- **QuantInsti blog** — backtesting bias, Kelly risk-constrained ([blog.quantinsti.com](https://blog.quantinsti.com/risk-constrained-kelly-criterion/)).
- **r/algotrading** — orderflow audits real, separar señal de hype.
- **r/PredictionMarkets**, **r/SportsBetting** — line shopping, CLV, EV positivo.
- **Polymarket Oracle blog** ([news.polymarket.com](https://news.polymarket.com/p/copycat)) — análisis interno wallets/copy.
- **PANews / MEXC News** — coberturas frecuentes Polymarket dynamics.
- **Sterling Crispin** (autor "Always NO" bot) — Twitter/X.
- **financemagnates.com** trending — frecuente cobertura prediction market microstructure.

---

## Plan de mejoras concreto basado en research

### PRIORIDAD ALTA (esta sprint)

1. **Kill switch obligatorio antes de cualquier nuevo deploy live**
   - Daily loss cap (ej -2% bankroll)
   - Consecutive loss cap (ej 5 trades en rojo → pause 1h)
   - Max drawdown cap (ej -10% all-time peak → halt manual)
   - Trigger debe escribir flag en Postgres + matar workers Celery.

2. **Spread/depth precheck antes de cualquier market order**
   - Antes de fill: leer book depth, abortar si liquidez dentro ±0.5% < 2x order size.
   - Skip signal si current spread > 1.5x rolling 20m median.
   - Migrar market orders a limit-with-max-slippage (CLOB lo soporta).

3. **CLV tracking**
   - Por cada trade, persistir precio entrada y precio cierre/resolución.
   - Métrica diaria: % trades con CLV positivo. Si <50% sostenido 14 días, edge no existe.

### PRIORIDAD MEDIA (próximas 2-3 semanas)

4. **Tick replay backtest engine**
   - Reemplazar dry-run actual con replay de WS feeds reales (ya capturás algunos).
   - Modelar slippage como función de order_size / book_depth_at_entry.
   - Validar: backtest engine debe reproducir live PnL ±10% en muestra de 30 días reales antes de confiar en él.

5. **Selector wallets v2 con scoring multi-señal**
   - Sumar a Nivel 1 (bucket scalper): holding period mediano, Sharpe individual, correlación PnL post-copy (si existe), diversidad de mercados, ratio markets >$30k liq.
   - Recalcular semanal. Wallets con cambio comportamiento brusco → drop automático.
   - Output: blacklist (HFT/scalper/wash sospecha) + watchlist scoring continuo.

6. **Position sizing con Half-Kelly explícito**
   - Por trade: f = 0.5 * (b·p − q) / b con p estimado del scoring del selector.
   - Cap absoluto: max 5% bankroll por trade independiente del Kelly (defensa contra estimation error).
   - Si f < 0.005, skip.

### PRIORIDAD BAJA (backlog research)

7. **News scraper minimalista** para detectar momentum windows (Twitter listas verificadas + RSS AP/Reuters). Solo trigger si market $vol > X y news contiene keyword del market title. Empezar manual antes de automatizar.

8. **XGBoost selector de markets** (no de precios)
   - Features: liquidez, spread, age, vol 1h, midpoint drift, bookpressure.
   - Target: trade abierto cierra en green vs red en 1h horizon.
   - Tiempo aware splits estrictos. Walk-forward retrain mensual.

9. **Hedge cross-venue** — ya en Nivel 2. Monitorear funding rate Binance perps + cost of capital para asegurar arb es post-fees positivo. Tracker fees/funding/slippage end-to-end.

10. **Auditar deps con `pip-audit` + `safety` weekly cron**. Pin versiones. Mirror packages críticos en private registry para evitar typo-squat / supply chain attacks.

---

## Sources

Comprehensive list (en hyperlinks dentro del doc):

- [fa-mag — bots win, retail loses](https://www.fa-mag.com/news/most-prediction-market-traders-are-losing-money-while-bots-rack-up-gains-86783.html)
- [Yahoo Finance — bots dominate Polymarket](https://finance.yahoo.com/news/arbitrage-bots-dominate-polymarket-millions-100000888.html)
- [Bitget — same coverage](https://www.bitget.com/news/detail/12560605132097)
- [QuantVPS — Polymarket HFT AI arbitrage](https://www.quantvps.com/blog/polymarket-hft-traders-use-ai-arbitrage-mispricing)
- [QuantVPS — Polymarket copy trading](https://www.quantvps.com/blog/polymarket-copy-trading-bot)
- [QuantVPS — automated trading on Polymarket](https://www.quantvps.com/blog/automated-trading-polymarket)
- [Medium 0xmega — wallet selection sin getting rekt](https://medium.com/@0xmega/how-to-find-the-best-polymarket-wallets-to-copy-trade-without-getting-rekt-26dd65123324)
- [Medium 0xmega — best copy bots 2026](https://medium.com/@0xmega/best-copy-trading-bots-to-make-500-day-on-polymarket-2026-comparison-29db38d7fdce)
- [Medium ILLUMINATION — 4 strategies bots actually profit](https://medium.com/illumination/beyond-simple-arbitrage-4-polymarket-strategies-bots-actually-profit-from-in-2026-ddacc92c5b4f)
- [Medium dexoryn — 7 arbitrage strategies](https://medium.com/@dexoryn/7-polymarket-arbitrage-strategies-every-trader-should-know-6d74b615b86e)
- [Medium PolyMaster — programmatically identifying arb](https://medium.com/@wanguolin/how-to-programmatically-identify-arbitrage-opportunities-on-polymarket-and-why-i-built-a-portfolio-23d803d6a74b)
- [PANews — smart money copy guide pitfalls](https://www.panewslab.com/en/articles/019d3235-40a0-764d-ab19-5a1d53ed9303)
- [Polycopytrade — whale wallets identification](https://www.polycopytrade.space/blog/polymarket-whale-wallets-copy-trading/)
- [Polymarket Oracle COPYCAT post](https://news.polymarket.com/p/copycat)
- [Polymarket Oracle COPYTRADE WARS](https://news.polymarket.com/p/copytrade-wars)
- [DLNews — bot-like bettors took millions](https://www.dlnews.com/articles/markets/polymarket-users-lost-millions-of-dollars-to-bot-like-bettors-over-the-past-year/)
- [Decrypt — "Always bets NO" bot](https://decrypt.co/364381/polymarket-bot-bets-no-has-a-point)
- [Frontierbeat — same Always-NO coverage](https://frontierbeat.com/2026/04/15/nothing-ever-happens-polymarket-bot-bets-no/)
- [DevGenius — weather trading bots Polymarket](https://blog.devgenius.io/found-the-weather-trading-bots-quietly-making-24-000-on-polymarket-and-built-one-myself-for-free-120bd34d6f09)
- [Finbold — Claude bot $1 → $3.3M](https://finbold.com/claude-ai-powered-trading-bot-turns-1-into-3-3-million-on-polymarket/)
- [CNBC — Polymarket launches perps](https://www.cnbc.com/2026/04/21/polymarket-launches-trading-of-heavily-leveraged-perps-contracts.html)
- [Bitcoinmagazine — Kalshi/Polymarket perps race](https://bitcoinmagazine.com/news/kalshi-and-polymarket-enter-the-crypto)
- [Hyperliquid prediction markets](https://www.dlnews.com/articles/markets/hyperliquid-launches-prediction-markets-for-bitcoin/)
- [financemagnates — bot playground](https://www.financemagnates.com/trending/prediction-markets-are-turning-into-a-bot-playground/)
- [financemagnates — prediction markets liquidity](https://www.financemagnates.com/fintech/prediction-markets-scale-up-as-volumes-surge-but-regulation-and-liquidity-remain-key-constraints/)
- [Kalena — order flow audit r/algotrading](https://blog.kalena.ai/crypto-algo-trading-reddit-the-order-flow-audit-stress-testing-the-7-most-upvoted-algorithmic-strategies-against-real-market-microstructure)
- [Luxalgo — backtesting traps](https://www.luxalgo.com/blog/backtesting-traps-common-errors-to-avoid/)
- [Luxalgo — slippage liquidity backtesting](https://www.luxalgo.com/blog/backtesting-limitations-slippage-and-liquidity-explained/)
- [Botjockie — backtest vs live](https://www.botjockie.com/blog/backtest-vs-live-trading.html)
- [QuantInsti — backtesting mistakes](https://blog.quantinsti.com/common-mistakes-backtesting/)
- [QuantInsti — risk-constrained Kelly](https://blog.quantinsti.com/risk-constrained-kelly-criterion/)
- [Lopez de Prado — Deflated Sharpe Ratio SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
- [Bailey — Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)
- [Wikipedia — Deflated Sharpe Ratio](https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio)
- [Altrady — Kelly Criterion crypto](https://www.altrady.com/blog/risk-management/kelly-criterion-crypto-position-sizing)
- [LBank — Kelly crypto risk mgmt](https://www.lbank.com/explore/mastering-the-kelly-criterion-for-smarter-crypto-risk-management)
- [Cripton AI — bot risk mgmt 2026](https://cripton.ai/en/guides/bot-risk-management)
- [Nadcab — stop loss / position sizing](https://www.nadcab.com/blog/trading-bot-risk-management-stop-loss-position-sizing-drawdown-control)
- [arxiv 2506.22055 — XGBoost+LSTM crypto](https://arxiv.org/abs/2506.22055)
- [Cube — slippage explained](https://www.cube.exchange/what-is/slippage)
- [Bitquery — MEV attacks types](https://bitquery.io/blog/different-mev-attacks)
- [a16zcrypto — MEV explained](https://a16zcrypto.com/posts/article/mev-explained/)
- [arxiv 2309.13648 — Don't let MEV slip Uniswap costs](https://arxiv.org/html/2309.13648v2)
- [Whale Alert](https://whale-alert.io/)
- [VoiceOfChain — Whale Alert Twitter use](https://voiceofchain.com/academy/whale-alerts-twitter)
- [Wundertrading — whale bot](https://wundertrading.com/journal/en/learn/article/crypto-whale-bot)
- [OddsJam — CLV explained](https://oddsjam.com/betting-education/closing-line-value)
- [Sharpfootballanalysis — CLV guide](https://www.sharpfootballanalysis.com/sportsbook/clv-betting/)
- [Pikkit — track CLV](https://pikkit.com/blog/how-to-track-closing-line-value-clv-in-sports-betting)
- [Bettoredge — line shopping](https://www.bettoredge.com/post/the-importance-of-line-shopping-in-sports-betting)
- [Phemex — wallet baskets strategy](https://phemex.com/news/article/innovative-strategy-emerges-for-polymarket-copy-trading-50622)
- [Quicknode — building copy trading bot](https://www.quicknode.com/guides/defi/polymarket-copy-trading-bot)
- [Pineconnector — bridging backtest live gap](https://www.pineconnector.com/blogs/pico-blog/backtesting-vs-live-trading-bridging-the-gap-between-strategy-and-reality)
- [Algopolis — why backtests fail real](https://algopolis.com/why-backtested-trading-strategies-fail-in-real-markets/)
- [Algotest — algo strategy failing reasons](https://algotest.in/blog/5-reasons-why-your-algo-trading-strategy-is-failing-and-how-to-fix-it/)
- [Blockchain Council — backtesting AI crypto safely](https://www.blockchain-council.org/cryptocurrency/backtesting-ai-crypto-trading-strategies-avoiding-overfitting-lookahead-bias-data-leakage/)
- [Freqtrade lookahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/)
- [TopAIHubs — bot that broke the market](https://topaihubs.com/articles/the-polymarket-bot-that-broke-the-market-lessons-for-ai-automation)
- [MEXC — why never copy HFT bot](https://www.mexc.com/news/1005336)
- [Cryptotradingbots.info — Kelly + price lag bots](https://cryptotradingbots.info/2026/03/19/polymarket-ai-trading-bots-using-kelly-criterion-and-price-lag-exploitation-for-high-win-rate-bets/)

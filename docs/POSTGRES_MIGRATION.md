# Postgres Migration Playbook

> **Estado al 2026-05-08**: bootstrap completo. Postgres corre en docker
> alongside del bot, con schema creado automáticamente en el primer arranque.
> El **switch a Postgres NO se hizo todavía** — server/runner siguen contra
> SQLite (`data/copybot.db`). Este doc explica cómo hacer el cutover cuando
> sea el momento.

## Por qué migrar

SQLite tiene un solo lock global de escritura. Cuando `discovery.run_cycle()`
abre una transacción larga (~10-30s para `recompute_clusters`), el polling
y el WS bridge esperan o tiran `database is locked`. A medida que el bot
escala (más wallets, más mercados, más métricas), el lock-time crece.

Postgres tiene row-level locks: dos transacciones que tocan filas distintas
no se bloquean. Eso resuelve el problema de raíz, pero cuesta **5-8 días
de trabajo** + ventana de migración.

Alternativas más baratas (evaluar primero):
- Romper `recompute_clusters` en chunks de 10 wallets (~30 min de trabajo,
  resuelve el 95% del lock).
- Subir `busy_timeout` y aceptar más latencia.

Si después de eso aún querés Postgres (queries analytics, dashboard
externo, escalar a millones de filas), seguí abajo.

## Qué ya está hecho (commit `<llenar>`)

- [x] `docker-compose.yml`: servicio `postgres:16-alpine` con healthcheck,
  volume `pgdata`, bind a `127.0.0.1:5432` (no expuesto a la red).
- [x] `scripts/postgres/schema.sql`: schema PG espejo del SQLite con todas
  las migrations inlineadas.
- [x] `scripts/postgres/migrate_data.py`: script idempotente que copia data
  SQLite → Postgres tabla por tabla, con `setval` de los sequences.
- [x] `pyproject.toml`: `psycopg[binary]>=3.2` como optional dep
  (`pip install -e ".[postgres]"`).
- [x] `.env.example`: vars `DB_BACKEND`, `POSTGRES_*`.

## Qué FALTA (el switch)

- [ ] Reescribir `src/db/schema.py` para soportar dual-backend. Hoy es
  100% sqlite3-specific (`PRAGMA`, `strftime`, etc.). Crear capa de
  abstracción mínima:
  - `db()`/`tx()` context managers que devuelven `psycopg.Connection`
    cuando `DB_BACKEND=postgres`.
  - Un placeholder marker (`?` SQLite, `%s` PG) — más simple: sed
    todas las queries para usar named params `:foo` (compatible con
    ambos via psycopg + sqlite3 con `paramstyle="named"`), o un
    helper `q(sql)` que reescribe.
  - **`row_factory`**: PG devuelve dicts si seteás `row_factory=dict_row`,
    SQLite usa `sqlite3.Row`. Ambos soportan `row["key"]` así que
    el codepath actual debería funcionar sin cambios.
- [ ] Auditar **cada query** en el codebase. Casos a buscar y reescribir:
  - `strftime('%s','now')` → `EXTRACT(EPOCH FROM NOW())::BIGINT`
  - `unixepoch()` → `EXTRACT(EPOCH FROM NOW())::BIGINT`
  - `datetime('now')` → `NOW()` (PG nativo)
  - `INSERT OR REPLACE` → `ON CONFLICT (...) DO UPDATE SET ...`
  - `MAX(CAST(x AS INTEGER))` → `GREATEST(x::BIGINT, ...)` o cast
    explícito; en PG el tipo importa.
  - Booleans: SQLite acepta `0/1`; PG es `BOOLEAN` (cuidado con
    `INTEGER` columns que el code asume `is_buy=1`).
- [ ] Tests pasen contra ambos backends. Idealmente parametrizar
  `conftest.py` con fixture `backend = ["sqlite", "postgres"]`.
- [ ] Backup obligatorio de SQLite ANTES del switch:
  `cp data/copybot.db data/copybot.db.pre-pg-$(date +%s).bak`

## Plan de cutover (cuando se decida hacerlo)

### Pre-requisitos

1. Tener este branch mergeado en `main` con todos los puntos de "FALTA"
   completados y testeados localmente.
2. Tests verdes contra Postgres en CI.
3. Ventana de mantenimiento de **30-60 min** acordada con el usuario.
4. Backup reciente de `data/copybot.db`.

### Pasos en Lenovo

```bash
# 0) Asumimos commit con switch ya está mergeado y la imagen subida a GHCR.

# 1) Pull del nuevo código + imagen
cd ~/polymarket_copybot/bot_trading
git pull origin main
docker compose pull

# 2) Setear POSTGRES_PASSWORD en .env (generar fuerte; NUNCA commitear)
echo "POSTGRES_PASSWORD=$(openssl rand -base64 32 | tr -d '/+=')" >> .env
# DB_BACKEND queda en sqlite por ahora — el switch es PASO 6.

# 3) Levantar postgres (server/runner siguen contra sqlite)
docker compose up -d postgres
docker compose logs postgres | grep "ready to accept"

# 4) Backup SQLite
cp data/copybot.db data/copybot.db.pre-pg-$(date +%Y%m%d-%H%M%S).bak

# 5) Migrar data SQLite → Postgres
docker compose exec -e PGPASSWORD=$(grep POSTGRES_PASSWORD .env | cut -d= -f2) postgres \
    pg_isready -U copybot -d copybot

# Instalar psycopg en el host (o correr el script desde un container temporal)
docker run --rm --network=bot_trading_default \
    -v $(pwd)/data:/data:ro \
    -v $(pwd)/scripts:/scripts:ro \
    python:3.11-slim sh -c "
        pip install 'psycopg[binary]>=3.2' &&
        python /scripts/postgres/migrate_data.py \
            --sqlite /data/copybot.db \
            --pg-dsn postgresql://copybot:$POSTGRES_PASSWORD@postgres:5432/copybot
    "

# 6) Validar (counts deben matchear)
# psql -U copybot -d copybot -h localhost -p 5432 \
#   -c "SELECT 'paper_trades' tbl, COUNT(*) FROM paper_trades
#       UNION SELECT 'trades', COUNT(*) FROM trades
#       UNION SELECT 'learning_events', COUNT(*) FROM learning_events;"

# 7) Switch backend
sed -i 's/^DB_BACKEND=sqlite/DB_BACKEND=postgres/' .env

# 8) Restart bot
docker compose restart server runner

# 9) Smoke check
curl http://localhost:8000/api/summary
curl http://localhost:8000/api/ws-status
docker compose logs --tail 50 runner | grep -E "ERROR|database"
```

### Plan de rollback

Si algo se rompe:

```bash
# Volver a SQLite (la DB original está intacta)
sed -i 's/^DB_BACKEND=postgres/DB_BACKEND=sqlite/' .env
docker compose restart server runner
# Postgres queda corriendo pero ignorado — borrarlo después si querés:
# docker compose down postgres && docker volume rm polymarket_copybot_pgdata
```

La data en SQLite sigue válida (el script de migración es read-only sobre
SQLite). Lo único perdido son los trades/eventos que pasaron en la ventana
post-switch — que igual se replican rápido por el polling.

## Estimación de esfuerzo del switch (post-bootstrap)

| Tarea | Tiempo |
|---|---|
| Capa dual-backend en schema.py | 1 día |
| Auditar y reescribir queries (~60 lugares) | 2-3 días |
| Tests pasan contra ambos backends | 1-2 días |
| Migración de data + smoke en Lenovo | 0.5 día |
| Buffer para sorpresas (FK constraints, type strictness) | 1 día |
| **Total** | **~5-8 días dedicados** |

## Notas operativas

- **Backups**: `pg_dump` diario al volume montado en `data/backups/`.
  Se puede agregar como cron en Lenovo o un service en docker-compose.
- **Acceso remoto**: el puerto 5432 está bind a `127.0.0.1` para no
  exponer la DB. Para conectarte desde Mac, hacer SSH tunnel:
  `ssh -L 5433:localhost:5432 melina@100.98.174.60` y conectar a
  `localhost:5433`.
- **Updates de imagen**: `postgres:16-alpine` es estable; cuando salga
  17 (probable Q4 2026) evaluar upgrade aparte (requiere `pg_upgrade`
  o pg_dump + restore).

## Decisiones pendientes

- [ ] ¿Mantener `DB_BACKEND` toggleable indefinidamente o eliminar el
  codepath SQLite tras 1 mes operativo en PG?
- [ ] ¿Migrar `raw` JSON columns a `JSONB` para queries analíticas
  (`raw->>'slug'`, etc)? Beneficio en queries del dashboard, costo
  cero porque ya estamos con texto JSON.
- [ ] ¿Postgres en otro VPS para no compartir CPU con el bot? El
  cluster K-means + el postgres bajo carga van a competir por CPU
  cuando el bot escale.

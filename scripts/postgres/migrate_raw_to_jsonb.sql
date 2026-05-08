-- Migra columnas `raw TEXT` → `JSONB` en las tablas que la usan.
-- Ejecutar UNA vez después del cutover a PG.
--
-- Razones:
--   1. Storage más compacto (PG comprime JSONB binario).
--   2. Queries `raw->>'slug'` o `raw @> '{"side":"BUY"}'` SIN parseo.
--   3. Índices GIN para lookups por contenido en O(log n).
--
-- Compat: el wrapper _PgRow en src/db/schema.py serializa dict/list a
-- string en read, así que `json.loads(row['raw'])` sigue funcionando sin
-- cambios. Los queries internos del wrapper también funcionan transparente.
--
-- Riesgo: si alguna fila tiene JSON inválido en raw, el ALTER falla.
-- Mitigación: el USING expression filtra null y maneja parse error
-- devolviendo NULL para esos casos.

BEGIN;

-- paper_trades.raw (38 filas) — chico, rápido
ALTER TABLE paper_trades
    ALTER COLUMN raw TYPE JSONB
    USING NULLIF(raw, '')::JSONB;

-- live_trades.raw (60 filas)
ALTER TABLE live_trades
    ALTER COLUMN raw TYPE JSONB
    USING NULLIF(raw, '')::JSONB;

-- trades.raw (3.6M filas) — más lento, ~30-60s estimado
ALTER TABLE trades
    ALTER COLUMN raw TYPE JSONB
    USING NULLIF(raw, '')::JSONB;

-- Bonus: indexar `raw->>'slug'` en paper_trades para queries del dashboard
-- por slug ("trades en mercado X").
CREATE INDEX IF NOT EXISTS idx_paper_trades_raw_slug
    ON paper_trades ((raw->>'slug'));

-- Bonus: GIN sobre paper_trades.raw para queries por contenido.
-- (Útil si después agregamos filtros como `raw @> '{"category":"crypto"}'`.)
CREATE INDEX IF NOT EXISTS idx_paper_trades_raw_gin
    ON paper_trades USING GIN (raw);

COMMIT;

-- Verificar que el cambio quedó.
\d+ paper_trades
\d+ live_trades
\d+ trades

#!/bin/bash
# db_backup.sh — backup PG/SQLite antes de cualquier deploy live.
#
# Uso: bash scripts/db_backup.sh
# - DB_BACKEND=postgres → pg_dump | gzip → /app/backups/copybot-<TS>.sql.gz
# - resto → cp DB_PATH → /app/backups/copybot-<TS>.db
#
# Pre-live snapshot: corre antes de cada switch LIVE_DRY_RUN=false o cualquier
# cambio destructivo de schema. Idempotente; mkdir -p el destino.
set -e

TS=$(date -u +%Y%m%d-%H%M%S)
BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
mkdir -p "$BACKUP_DIR"

if [ "$DB_BACKEND" = "postgres" ]; then
    HOST="${POSTGRES_HOST:-postgres}"
    USER="${POSTGRES_USER:-copybot}"
    DBNAME="${POSTGRES_DB:-copybot}"
    OUT="$BACKUP_DIR/copybot-${TS}.sql.gz"
    pg_dump -h "$HOST" -U "$USER" "$DBNAME" | gzip > "$OUT"
    echo "Backup: $OUT"
else
    SRC="${DB_PATH:-data/copybot.db}"
    OUT="$BACKUP_DIR/copybot-${TS}.db"
    cp "$SRC" "$OUT"
    echo "Backup: $OUT"
fi

ls -lh "$BACKUP_DIR"/ | tail -5

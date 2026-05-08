#!/bin/sh
# Backup loop ejecutado dentro del container `postgres-backup`.
#
# Cada BACKUP_INTERVAL_S segundos (default 24h):
#   1. pg_dump comprimido del DB → /backups/copybot-YYYYMMDD-HHMMSS.sql.gz
#   2. Rota: borra backups > BACKUP_RETENTION_DAYS días
#
# Las vars POSTGRES_* se heredan del docker-compose. Si falla un dump,
# loggea el error y reintenta en el próximo ciclo (no muere el container).
set -eu

BACKUP_DIR=${BACKUP_DIR:-/backups}
BACKUP_INTERVAL_S=${BACKUP_INTERVAL_S:-86400}      # 24h
BACKUP_RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-7}

mkdir -p "$BACKUP_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

run_backup() {
    local ts file
    ts=$(date '+%Y%m%d-%H%M%S')
    file="$BACKUP_DIR/copybot-${ts}.sql.gz"
    log "starting backup → $file"
    if PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
            --host="${POSTGRES_HOST:-postgres}" \
            --port="${POSTGRES_PORT:-5432}" \
            --username="${POSTGRES_USER:-copybot}" \
            --dbname="${POSTGRES_DB:-copybot}" \
            --no-owner \
            --no-privileges \
            --format=plain \
            | gzip > "$file"; then
        sz=$(du -h "$file" | cut -f1)
        log "backup OK ($sz)"
    else
        log "backup FAILED — borrando archivo parcial"
        rm -f "$file"
        return 1
    fi
}

rotate() {
    local n
    # find -mtime +N: archivos modificados hace MÁS de N días
    n=$(find "$BACKUP_DIR" -maxdepth 1 -name "copybot-*.sql.gz" \
        -mtime +"$BACKUP_RETENTION_DAYS" -print | wc -l)
    if [ "$n" -gt 0 ]; then
        log "rotando $n backups > ${BACKUP_RETENTION_DAYS}d"
        find "$BACKUP_DIR" -maxdepth 1 -name "copybot-*.sql.gz" \
            -mtime +"$BACKUP_RETENTION_DAYS" -delete
    fi
}

# Backup inicial inmediato (smoke test que credenciales OK).
log "postgres-backup loop arrancando — interval=${BACKUP_INTERVAL_S}s retention=${BACKUP_RETENTION_DAYS}d"
run_backup || log "WARNING: primer backup falló — reintentando en próximo ciclo"
rotate

while true; do
    sleep "$BACKUP_INTERVAL_S"
    run_backup || log "WARNING: backup falló — sigue el loop"
    rotate
done

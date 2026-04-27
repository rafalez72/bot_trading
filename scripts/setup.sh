#!/usr/bin/env bash
# Setup portable: Mac y Linux. Crea .venv, instala deps, copia .env.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 no está instalado."
    echo "  Mac:    brew install python@3.12"
    echo "  Ubuntu: sudo apt install python3 python3-venv python3-pip"
    exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "→ Python $PY_VER detectado"

if [ ! -d ".venv" ]; then
    echo "→ Creando entorno virtual…"
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "→ Actualizando pip…"
python -m pip install --quiet --upgrade pip

echo "→ Instalando dependencias…"
pip install --quiet -e ".[analytics,dashboard]"

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "→ .env creado desde .env.example"
fi

echo "→ Inicializando base SQLite…"
python copybot.py init

echo ""
echo "✅ Setup completo."
echo ""
echo "Comandos para empezar:"
echo "  source .venv/bin/activate"
echo "  python copybot.py markets        # indexa mercados"
echo "  python copybot.py discover       # descubre wallets activos"
echo "  python copybot.py status         # ver progreso"

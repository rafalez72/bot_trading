#!/bin/bash
# Security audit local — corre semanal en CI (.github/workflows/security-audit.yml)
# y on-demand antes de merges grandes.
#
# Background: Dic-2025 hubo malware en bots Polymarket vía deps comprometidas;
# Ene-2026 typo-squat en PyPI (paquetes con nombres similares a populares).
# Este script verifica:
#   1. CVEs conocidos en deps actuales (pip-audit)
#   2. Cross-check con safety (DB alternativa, opcional)
#   3. Lista deps desactualizadas (señal temprana de paquetes abandonados)
set -e

echo "=== pip-audit ==="
# pip-audit lee pyproject.toml si está; fallback a requirements/installed
pip-audit --requirement pyproject.toml || pip-audit

echo ""
echo "=== safety check (alt) ==="
safety check 2>/dev/null || echo "safety not installed (skip)"

echo ""
echo "=== outdated deps ==="
pip list --outdated

echo ""
echo "=== done ==="

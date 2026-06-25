#!/bin/bash
# install.sh — Configura el entorno del cliente de deploy (espbench).
# Crea un venv en client/.venv e instala dependencias.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

echo "[install] Creando venv en $VENV..."
python3 -m venv "$VENV"

echo "[install] Instalando dependencias..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "============================================="
echo "  espbench client — instalacion OK"
echo "============================================="
echo "  Para usar deploy:"
echo "    $VENV/bin/python $SCRIPT_DIR/deploy.py"
echo "  O agregá el venv al PATH:"
echo "    source $VENV/bin/activate"
echo "    python deploy.py"
echo "============================================="

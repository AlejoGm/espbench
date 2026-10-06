#!/bin/bash
# install.sh — Configura el cliente de espbench (idempotente).
# - venv en client/.venv con las dependencias de deploy.py (rich)
# - el CLI `espbench` en ~/.local/bin (o $ESPBENCH_BIN_DIR): un wrapper que corre client/espbench.py
#
# Overrides (tests): ESPBENCH_VENV, ESPBENCH_BIN_DIR, ESPBENCH_SKIP_PIP=1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${ESPBENCH_VENV:-$SCRIPT_DIR/.venv}"
BIN_DIR="${ESPBENCH_BIN_DIR:-$HOME/.local/bin}"

echo "[install] Creando venv en $VENV..."
python3 -m venv "$VENV"

if [ "${ESPBENCH_SKIP_PIP:-0}" != "1" ]; then
    echo "[install] Instalando dependencias..."
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
fi

# El CLI no tiene dependencias (urllib): el wrapper usa el python del venv igual,
# así deploy y espbench corren con el mismo intérprete.
echo "[install] Instalando el CLI espbench en $BIN_DIR..."
mkdir -p "$BIN_DIR"
WRAPPER="$BIN_DIR/espbench"
TMP="$(mktemp "$BIN_DIR/.espbench.XXXXXX")"
cat > "$TMP" <<WRAP
#!/bin/sh
# Generado por $SCRIPT_DIR/install.sh — no editar (se regenera).
exec "$VENV/bin/python" "$SCRIPT_DIR/espbench.py" "\$@"
WRAP
chmod 755 "$TMP"
mv -f "$TMP" "$WRAPPER"

echo ""
echo "============================================="
echo "  espbench client — instalacion OK"
echo "============================================="
echo "  deploy (humanos):  $VENV/bin/python $SCRIPT_DIR/deploy.py"
echo "  CLI (agentes):     espbench --help"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "  ⚠ $BIN_DIR no está en el PATH: agregalo (export PATH=\"$BIN_DIR:\$PATH\")" ;;
esac
echo "  Skill para Claude Code:"
echo "    ln -sfn $SCRIPT_DIR/agent ~/.claude/skills/espbench"
echo "============================================="

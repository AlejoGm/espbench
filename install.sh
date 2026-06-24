#!/bin/bash
# install.sh — Deploy remoteFlashServer to a Raspberry Pi.
# Idempotent: safe to run multiple times.
# Must run as root.
set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo "[INFO]  $*"; }
warn()  { echo "[WARN]  $*"; }
die()   { echo "[ERROR] $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Root check
# ---------------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    die "Este script debe ejecutarse como root (usa sudo)."
fi

# ---------------------------------------------------------------------------
# 2. Dependency check
# ---------------------------------------------------------------------------
for cmd in tmux python3 pip3; do
    if ! command -v "$cmd" &>/dev/null; then
        die "Dependencia faltante: '$cmd' no encontrado en PATH. Instalalo antes de continuar."
    fi
done
info "Dependencias OK (tmux, python3, pip3)."

# ---------------------------------------------------------------------------
# Script location — all source paths are relative to the repo root,
# which is the directory that contains this script.
# ---------------------------------------------------------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# 3. Create directory structure
# ---------------------------------------------------------------------------
info "Creando directorios..."
mkdir -p /opt/esp/server /opt/esp/logs /opt/esp/jobs /opt/esp/dashboard
chmod 777 /opt/esp/logs

# ---------------------------------------------------------------------------
# 4. Create venv and install Python dependencies
# ---------------------------------------------------------------------------
info "Creando venv en /opt/esp/venv..."
python3 -m venv /opt/esp/venv
info "Instalando dependencias Python..."
/opt/esp/venv/bin/pip install --quiet -r "$REPO_DIR/requirements.txt"

# ---------------------------------------------------------------------------
# 5. Copy server files
# ---------------------------------------------------------------------------
info "Copiando server/ -> /opt/esp/server/..."
cp -r "$REPO_DIR/server/"* /opt/esp/server/

# ---------------------------------------------------------------------------
# 6. Copy common.py
# ---------------------------------------------------------------------------
info "Copiando common.py -> /opt/esp/common.py..."
cp "$REPO_DIR/common.py" /opt/esp/common.py

# ---------------------------------------------------------------------------
# 6b. Copy dashboard static files
# ---------------------------------------------------------------------------
info "Copiando dashboard/ -> /opt/esp/dashboard/..."
cp -r "$REPO_DIR/dashboard/"* /opt/esp/dashboard/

# ---------------------------------------------------------------------------
# 6. Install devremote CLI
# ---------------------------------------------------------------------------
info "Instalando devremote en /usr/local/bin/..."
cp "$REPO_DIR/infra/devremote" /usr/local/bin/devremote
chmod +x /usr/local/bin/devremote

# ---------------------------------------------------------------------------
# 7. Install esp32_tmux.sh
# ---------------------------------------------------------------------------
info "Instalando esp32_tmux.sh en /usr/local/bin/..."
cp "$REPO_DIR/infra/esp32_tmux.sh" /usr/local/bin/esp32_tmux.sh
chmod +x /usr/local/bin/esp32_tmux.sh

# ---------------------------------------------------------------------------
# 8. Install udev rules
# ---------------------------------------------------------------------------
info "Instalando reglas udev..."
cp "$REPO_DIR/infra/99-esp32.rules" /etc/udev/rules.d/99-esp32.rules

# ---------------------------------------------------------------------------
# 9. Install systemd services
# ---------------------------------------------------------------------------
info "Instalando servicio systemd devremote..."
cp "$REPO_DIR/infra/devremote.service" /etc/systemd/system/devremote.service

info "Instalando servicio systemd dashboard..."
cp "$REPO_DIR/infra/dashboard.service" /etc/systemd/system/dashboard.service

# ---------------------------------------------------------------------------
# 10. Reload udev
# ---------------------------------------------------------------------------
info "Recargando reglas udev..."
udevadm control --reload-rules
udevadm trigger

# ---------------------------------------------------------------------------
# 11. Enable systemd services
# ---------------------------------------------------------------------------
info "Habilitando servicios systemd..."
systemctl daemon-reload
systemctl enable devremote
systemctl enable dashboard

# ---------------------------------------------------------------------------
# 12. Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "  remoteFlashServer — instalacion OK"
echo "========================================"
echo "  Archivos instalados:"
echo "    /opt/esp/server/          <- servidor"
echo "    /opt/esp/dashboard/       <- dashboard frontend"
echo "    /opt/esp/common.py        <- modulo compartido"
echo "    /opt/esp/logs/            <- logs"
echo "    /opt/esp/jobs/            <- jobs temporales"
echo "    /usr/local/bin/devremote  <- CLI"
echo "    /usr/local/bin/esp32_tmux.sh"
echo "    /etc/udev/rules.d/99-esp32.rules"
echo "    /etc/systemd/system/devremote.service"
echo "    /etc/systemd/system/dashboard.service"
echo ""
echo "  Servicios habilitados (no iniciados)."
echo "  Para iniciar:    systemctl start devremote"
echo "                   systemctl start dashboard"
echo "  Dashboard:       http://<host>:8080/"
echo "  Para ver estado: devremote --status"
echo "========================================"

#!/bin/bash
# update.sh — Actualiza espbench en la Pi: fetch + pull + reinstall + reinicio.
# Corre desde el clone del repo en la Pi (no desde /opt/esp).
# Idempotente. Debe ejecutarse como root (usa sudo).
set -euo pipefail

info()  { echo "[INFO]  $*"; }
warn()  { echo "[WARN]  $*"; }
die()   { echo "[ERROR] $*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
    die "Este script debe ejecutarse como root (usa sudo)."
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

git rev-parse --git-dir &>/dev/null || die "No es un repo git: $REPO_DIR"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
info "Repo: $REPO_DIR"
info "Rama actual: $BRANCH"

if [ -n "$(git status --porcelain)" ]; then
    die "Hay cambios locales sin commitear en $REPO_DIR — resolvé eso antes de actualizar (git status)."
fi

info "Buscando cambios en origin..."
git fetch origin

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH" 2>/dev/null)" || die "La rama '$BRANCH' no tiene upstream en origin."

if [ "$LOCAL" = "$REMOTE" ]; then
    info "Ya estás al día ($LOCAL) — nada para traer."
else
    info "Trayendo cambios: ${LOCAL:0:7} → ${REMOTE:0:7}"
    git pull origin "$BRANCH"
fi

info "Instalando (remote/install.sh)..."
bash remote/install.sh

info "Reiniciando dashboard..."
systemctl restart dashboard

info "Reseteando sesiones (mata + relanza remote_esp32.py con el código nuevo)..."
devremote --reset

echo ""
info "Estado final:"
devremote --status

info "=== update completo ==="

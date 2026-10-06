#!/bin/bash
# update.sh — actualizar el bench desde el clone. Atajo de espbench-update (mismo script, el del repo):
#
#   sudo bash remote/infra/update.sh            la rama en la que está el clone (como antes); en un
#                                               release o detached: lo que diga update.conf (PIN o release)
#   sudo bash remote/infra/update.sh <ref>      a esa rama, tag o commit (queda fijo: PIN)
#   sudo bash remote/infra/update.sh --release  al último release y vuelve a seguir los releases
#
# Con el bench ya instalado alcanza con `sudo espbench-update` desde cualquier lado.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "[ERROR] correr como root (sudo)" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export ESPBENCH_REPO_DIR="$REPO_DIR"     # para la primera vez, antes de que exista update.conf

if [ $# -eq 0 ] && branch="$(git -C "$REPO_DIR" symbolic-ref -q --short HEAD)"; then
    set -- --ref "$branch"
elif [ $# -eq 1 ] && [ "${1#-}" = "$1" ]; then
    set -- --ref "$1"
fi
exec bash "$REPO_DIR/remote/infra/espbench-update" "$@"

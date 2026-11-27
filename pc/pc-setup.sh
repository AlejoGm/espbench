#!/bin/bash
# pc-setup.sh — Ubuntu (Server o Desktop, x86_64) como bench de espbench. Lo que en
# una Pi hace rpi/pi-setup.sh; después va `sudo bash remote/install.sh` igual que en la Pi.
#
# Cubre:
#   - Sistema al día (apt full-upgrade) y los paquetes que pide install.sh + SSH y mDNS
#   - Sin brltty ni ModemManager: en Ubuntu Desktop se adueñan de los puertos serie
#     (brltty toma los CH340; ModemManager manda comandos AT a cada ttyUSB/ttyACM nuevo)
#   - Nunca suspender (ni GNOME ni la pantalla de login de GDM a los 20 min)
#   - Hostname (opcional) y Tailscale con tag:bench
#
# No toca el escritorio gráfico: si se quiere sin él, `sudo systemctl set-default
# multi-user.target` (vuelve con graphical.target).
#
# Idempotente. Requiere root. Variables (todas opcionales), por entorno:
#   BENCH_HOSTNAME  nombre de la máquina (el del bench en bench-master y en Tailscale)
#   BENCH_TZ        zona horaria, p. ej. America/Argentina/Buenos_Aires
#   TS_AUTHKEY      auth key de Tailscale (un solo uso, no efímera, tag:bench): por
#                   entorno y no como argumento, para que no quede en `ps` ni en el historial
#
#   sudo BENCH_HOSTNAME=sensibox TS_AUTHKEY=tskey-auth-... bash pc/pc-setup.sh

set -euo pipefail

info() { echo "[INFO]  $*"; }
warn() { echo "[WARN]  $*"; }
die()  { echo "[ERROR] $*" >&2; exit 1; }
ok()   { echo "[OK]    $*"; }

[[ $EUID -ne 0 ]] && die "Ejecutar como root: sudo bash pc/pc-setup.sh"
[[ "$(uname -m)" == "x86_64" ]] || warn "Pensado para x86_64 (esto es $(uname -m)); en una Pi va rpi/pi-setup.sh."
command -v apt-get &>/dev/null || die "Hace falta apt (Ubuntu/Debian)."

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a          # needrestart (Ubuntu 24.04+) no abre su diálogo a mitad del upgrade

# ---------------------------------------------------------------------------
# 1. Sistema al día + paquetes
# ---------------------------------------------------------------------------
info "Actualizando el sistema (puede tardar)..."
apt-get update
apt-get -y full-upgrade
info "Instalando paquetes..."
apt-get install -y git tmux python3 python3-pip python3-venv wget curl \
    openssh-server avahi-daemon usbutils htop
ok "Paquetes instalados"

# ---------------------------------------------------------------------------
# 2. Puertos serie libres
# ---------------------------------------------------------------------------
info "Sacando brltty y ModemManager..."
apt-get purge -y brltty modemmanager || true
ok "brltty y ModemManager fuera"

# ---------------------------------------------------------------------------
# 3. Nunca suspender
# ---------------------------------------------------------------------------
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
ok "Suspensión e hibernación deshabilitadas"

# ---------------------------------------------------------------------------
# 4. Zona horaria y hostname (opcionales)
# ---------------------------------------------------------------------------
if [[ -n "${BENCH_TZ:-}" ]]; then
    timedatectl set-timezone "$BENCH_TZ"
    ok "Zona horaria: $BENCH_TZ"
fi
if [[ -n "${BENCH_HOSTNAME:-}" && "$(hostname)" != "$BENCH_HOSTNAME" ]]; then
    old="$(hostname)"
    hostnamectl set-hostname "$BENCH_HOSTNAME"
    sed -i "s/\b${old}\b/${BENCH_HOSTNAME}/g" /etc/hosts
    ok "Hostname: $old → $BENCH_HOSTNAME"
fi
# Evita "sudo: unable to resolve host"
HOSTNAME_NOW="$(hostname)"
grep -qw "$HOSTNAME_NOW" /etc/hosts || echo "127.0.1.1 $HOSTNAME_NOW" >> /etc/hosts

# ---------------------------------------------------------------------------
# 5. Tailscale
# ---------------------------------------------------------------------------
if command -v tailscale &>/dev/null; then
    info "Tailscale ya está instalado"
else
    info "Instalando Tailscale..."
    curl -fsSL https://tailscale.com/install.sh | sh
fi
systemctl enable --now tailscaled
ok "Tailscale instalado"

# Bench = nodo con tag:bench, no a nombre de un usuario (docs/security.md).
TS_TAGGED=0
if [[ -n "${TS_AUTHKEY:-}" ]]; then
    info "Conectando a Tailscale con tag:bench..."
    tailscale up --auth-key="$TS_AUTHKEY" --advertise-tags=tag:bench
    unset TS_AUTHKEY
    TS_TAGGED=1
    ok "Tailscale conectado"
fi

# ---------------------------------------------------------------------------
# Resumen
# ---------------------------------------------------------------------------
echo ""
echo "========================================================"
echo "  pc-setup.sh — OK ($(hostname))"
echo "========================================================"
echo ""
if [[ "$TS_TAGGED" != 1 ]]; then
echo "  TAILSCALE (una sola vez), una de dos:"
echo "    - Con login: sudo tailscale up --advertise-tags=tag:bench"
echo "      (abrís el link; tu usuario tiene que poder asignar tag:bench)"
echo "    - Con auth key (un solo uso, no efímera, tag:bench):"
echo "      sudo tailscale up --auth-key=<clave> --advertise-tags=tag:bench"
echo ""
fi
echo "  Siguiente: sudo bash remote/install.sh"
if [[ -f /var/run/reboot-required ]]; then
echo "  Después reiniciar (kernel nuevo): sudo reboot"
fi
echo "========================================================"

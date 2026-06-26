#!/bin/bash
# pi-setup.sh — Raspberry Pi OS hardening for espbench remote.
#
# Cubre:
#   - Sin desktop (multi-user.target)
#   - Serial TTL deshabilitado
#   - Teclado/mouse bloqueados (udev HID)
#   - WiFi provisioning con AP fallback (wifi-connect)
#   - Tailscale VPN
#   - Servicios innecesarios deshabilitados
#
# Idempotente. Seguro correr más de una vez.
# Requiere root. Reiniciar al terminar para que todos los cambios tomen efecto.

set -euo pipefail

# ---------------------------------------------------------------------------
# CONFIGURACIÓN — editar antes de correr
# ---------------------------------------------------------------------------

# WiFi provisioning: SSID y contraseña del AP de configuración
AP_SSID="SfyBench"
AP_PASS="1234567890"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info() { echo "[INFO]  $*"; }
warn() { echo "[WARN]  $*"; }
die()  { echo "[ERROR] $*" >&2; exit 1; }
ok()   { echo "[OK]    $*"; }

[[ $EUID -ne 0 ]] && die "Ejecutar como root: sudo bash pi-setup.sh"

# Fix: asegurar que el hostname esté en /etc/hosts (evita "sudo: unable to resolve host")
HOSTNAME=$(hostname)
if ! grep -q "$HOSTNAME" /etc/hosts; then
    echo "127.0.1.1 $HOSTNAME" >> /etc/hosts
    info "Hostname '$HOSTNAME' agregado a /etc/hosts"
fi

# Detectar partición de boot (Bullseye: /boot, Bookworm: /boot/firmware)
if [[ -f /boot/firmware/cmdline.txt ]]; then
    BOOT_DIR="/boot/firmware"
elif [[ -f /boot/cmdline.txt ]]; then
    BOOT_DIR="/boot"
else
    die "No se encontró cmdline.txt en /boot ni /boot/firmware"
fi
info "Boot dir: $BOOT_DIR"

# ---------------------------------------------------------------------------
# 1. Sin desktop
# ---------------------------------------------------------------------------
info "Configurando boot sin desktop..."
systemctl set-default multi-user.target
ok "Boot target: multi-user (CLI)"

# ---------------------------------------------------------------------------
# 2. Deshabilitar serial TTL
# ---------------------------------------------------------------------------
info "Deshabilitando serial TTL..."

sed -i 's/console=serial0,[0-9]* //g'  "$BOOT_DIR/cmdline.txt"
sed -i 's/console=ttyAMA0,[0-9]* //g' "$BOOT_DIR/cmdline.txt"
sed -i 's/console=ttyS0,[0-9]* //g'   "$BOOT_DIR/cmdline.txt"

if grep -q "^enable_uart=" "$BOOT_DIR/config.txt" 2>/dev/null; then
    sed -i 's/^enable_uart=.*/enable_uart=0/' "$BOOT_DIR/config.txt"
else
    echo "enable_uart=0" >> "$BOOT_DIR/config.txt"
fi

for svc in serial-getty@ttyAMA0.service serial-getty@ttyS0.service; do
    systemctl disable "$svc" 2>/dev/null && info "Deshabilitado: $svc" || true
done
ok "Serial TTL deshabilitado"

# ---------------------------------------------------------------------------
# 3. Bloquear teclado y mouse (HID USB)
# ---------------------------------------------------------------------------
info "Bloqueando dispositivos HID USB..."

cat > /etc/udev/rules.d/99-block-hid.rules << 'EOF'
# Bloquear teclado y mouse USB — espbench Pi appliance
SUBSYSTEM=="input", ATTRS{bInterfaceClass}=="03", OPTIONS+="ignore_device"
EOF

udevadm control --reload-rules
ok "Regla udev HID instalada"

# ---------------------------------------------------------------------------
# 4. NetworkManager (requerido por wifi-connect)
# ---------------------------------------------------------------------------
info "Verificando NetworkManager..."
if ! command -v nmcli &>/dev/null; then
    info "Instalando NetworkManager..."
    apt-get update -qq
    apt-get install -y network-manager
fi

if systemctl is-enabled dhcpcd &>/dev/null; then
    warn "Deshabilitando dhcpcd — NetworkManager toma el control."
    warn "Si conectaste via SSH/WiFi con dhcpcd, la conexión puede caerse."
    warn "Continuar? [s/N]"
    read -r CONFIRM
    [[ "$CONFIRM" =~ ^[sS]$ ]] || die "Abortado por usuario."
    systemctl disable dhcpcd 2>/dev/null || true
    systemctl stop dhcpcd 2>/dev/null || true
fi
systemctl enable NetworkManager
systemctl start NetworkManager
ok "NetworkManager activo"

# ---------------------------------------------------------------------------
# 5. WiFi provisioning — wifi-connect (Balena)
# ---------------------------------------------------------------------------
info "Instalando wifi-connect..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v wifi-connect &>/dev/null; then
    WC_RELEASE=$(curl -fsSL https://api.github.com/repos/balena-os/wifi-connect/releases/latest)
    WC_BIN_URL=$(echo "$WC_RELEASE" | grep "browser_download_url.*aarch64-unknown" | cut -d '"' -f 4)
    WC_UI_URL=$(echo "$WC_RELEASE"  | grep "browser_download_url.*wifi-connect-ui"  | cut -d '"' -f 4)
    [[ -z "$WC_BIN_URL" ]] && die "No se pudo obtener URL de wifi-connect desde GitHub API"
    info "Descargando wifi-connect..."
    mkdir -p /opt/wifi-connect
    curl -fsSL "$WC_BIN_URL" | tar xz -C /opt/wifi-connect/
    curl -fsSL "$WC_UI_URL"  | tar xz -C /opt/wifi-connect/
    chmod +x /opt/wifi-connect/wifi-connect
    ln -sf /opt/wifi-connect/wifi-connect /usr/local/bin/wifi-connect
fi

# Portal UI custom
if [[ -f "$SCRIPT_DIR/portal/index.html" ]]; then
    cp "$SCRIPT_DIR/portal/index.html" /opt/wifi-connect/index.html
    ok "Portal custom instalado"
else
    warn "portal/index.html no encontrado — usando UI de Balena"
fi
ok "wifi-connect instalado: $(wifi-connect --version 2>/dev/null || echo 'ok')"

cat > /usr/local/bin/wifi-provision.sh << EOSCRIPT
#!/bin/bash
sleep 15

# Verificar si wlan0 tiene conectividad (ignorar ethernet)
if ping -c 2 -W 5 -I wlan0 8.8.8.8 &>/dev/null; then
    exit 0
fi

exec wifi-connect \
    --portal-ssid "${AP_SSID}" \
    --portal-passphrase "${AP_PASS}" \
    --ui-directory /opt/wifi-connect
EOSCRIPT
chmod +x /usr/local/bin/wifi-provision.sh

cat > /etc/systemd/system/wifi-provision.service << 'EOF'
[Unit]
Description=WiFi provisioning — AP fallback si no hay internet
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
ExecStart=/usr/local/bin/wifi-provision.sh
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable wifi-provision.service
ok "WiFi provisioning habilitado (AP: '$AP_SSID')"

# ---------------------------------------------------------------------------
# 6. Deshabilitar servicios innecesarios
# ---------------------------------------------------------------------------
info "Deshabilitando servicios innecesarios..."
for svc in bluetooth avahi-daemon triggerhappy; do
    systemctl disable "$svc" 2>/dev/null && info "  - $svc deshabilitado" || true
done
ok "Servicios innecesarios deshabilitados"

# ---------------------------------------------------------------------------
# 7. Tailscale
# ---------------------------------------------------------------------------
info "Instalando Tailscale..."
curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable tailscaled
ok "Tailscale instalado"

# ---------------------------------------------------------------------------
# Resumen
# ---------------------------------------------------------------------------
echo ""
echo "========================================================"
echo "  pi-setup.sh — OK"
echo "========================================================"
echo ""
echo "  Flujo en próximo boot:"
echo "    1. NetworkManager intenta WiFi guardado"
echo "    2. Sin internet → AP '$AP_SSID' para configurar WiFi"
echo "    3. Con internet → Tailscale conecta solo"
echo ""
echo "  PASO MANUAL (una sola vez antes de desplegar):"
echo "    tailscale up"
echo "    → abrí el link en browser → autenticá → listo"
echo "    Reboots futuros reconectan solos."
echo ""
echo "  Reiniciar para aplicar todos los cambios:"
echo "    sudo reboot"
echo "========================================================"

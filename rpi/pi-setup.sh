#!/bin/bash
# pi-setup.sh — Raspberry Pi OS hardening for espbench remote.
#
# Cubre:
#   - Sin desktop (multi-user.target)
#   - Serial TTL deshabilitado
#   - Teclado/mouse bloqueados (udev HID)
#   - WiFi provisioning con AP fallback (wifi-connect) solo si wlan0 no tiene red
#   - WiFi sin power save, journal persistente
#   - Tailscale VPN
#   - Servicios innecesarios deshabilitados
#
# Idempotente. Seguro correr más de una vez.
# Requiere root. Reiniciar al terminar para que todos los cambios tomen efecto.
#
# Tailscale: con TS_AUTHKEY el bench entra a la tailnet solo, con tag:bench
# (ver docs/security.md). La clave va por variable de entorno, no por argumento
# (quedaría en `ps` y en el historial) y no se escribe a disco:
#   sudo TS_AUTHKEY=tskey-auth-... bash rpi/pi-setup.sh

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

if ! command -v wifi-connect &>/dev/null || [[ ! -d /opt/wifi-connect ]]; then
    WC_RELEASE=$(curl -fsSL https://api.github.com/repos/balena-os/wifi-connect/releases/latest)
    WC_BIN_URL=$(echo "$WC_RELEASE" | grep "browser_download_url.*aarch64-unknown" | cut -d '"' -f 4)
    [[ -z "$WC_BIN_URL" ]] && die "No se pudo obtener URL de wifi-connect desde GitHub API"
    info "Descargando wifi-connect..."
    mkdir -p /opt/wifi-connect
    curl -fsSL "$WC_BIN_URL" | tar xz -C /opt/wifi-connect/
    chmod +x /opt/wifi-connect/wifi-connect
    ln -sf /opt/wifi-connect/wifi-connect /usr/local/bin/wifi-connect
fi

# Portal UI custom
if [[ -f "$SCRIPT_DIR/portal/index.html" ]]; then
    mkdir -p /opt/wifi-connect
    cp "$SCRIPT_DIR/portal/index.html" /opt/wifi-connect/index.html
    ok "Portal custom instalado"
else
    warn "portal/index.html no encontrado — usando UI de Balena"
fi
ok "wifi-connect instalado: $(wifi-connect --version 2>/dev/null || echo 'ok')"

# El AP sale solo si wlan0 no tiene red (no por un corte de internet): ver rpi/wifi-provision.sh
install -m 755 "$SCRIPT_DIR/wifi-provision.sh" /usr/local/bin/wifi-provision.sh
# Perfiles vacíos que deja wifi-connect al cortarse la Pi con el AP prendido
find /etc/NetworkManager/system-connections -name "${AP_SSID}*.nmconnection" -size 0 -delete

cat > /etc/systemd/system/wifi-provision.service << EOF
[Unit]
Description=WiFi provisioning — AP de configuración si wlan0 no tiene red
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
Environment=AP_SSID=${AP_SSID}
Environment=AP_PASS=${AP_PASS}
ExecStart=/usr/local/bin/wifi-provision.sh
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable wifi-provision.service
systemctl try-restart wifi-provision.service
ok "WiFi provisioning habilitado (AP: '$AP_SSID')"

# Power save del WiFi apagado: con brcmfmac en power save el link pierde paquetes y tiene picos de
# latencia. Aplica a la próxima conexión; en caliente, con iw.
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/espbench-wifi-powersave.conf << 'EOF'
[connection]
wifi.powersave = 2
EOF
iw dev wlan0 set power_save off 2>/dev/null || true
ok "WiFi power save apagado"

# Journal persistente (acotado): sin esto, lo que pasó antes de un reinicio se pierde.
mkdir -p /etc/systemd/journald.conf.d /var/log/journal
cat > /etc/systemd/journald.conf.d/espbench.conf << 'EOF'
[Journal]
Storage=persistent
SystemMaxUse=200M
EOF
systemctl restart systemd-journald
ok "Journal persistente (máx. 200 MB)"

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
systemctl enable --now tailscaled
ok "Tailscale instalado"

# Bench = nodo con tag:bench, no a nombre de un usuario: no hereda sus permisos y
# la ACL no le deja iniciar conexiones a nadie (docs/security.md).
TS_TAGGED=0
if [[ -n "${TS_AUTHKEY:-}" ]]; then
    info "Conectando a Tailscale con tag:bench..."
    tailscale up --auth-key="$TS_AUTHKEY" --advertise-tags=tag:bench
    unset TS_AUTHKEY
    TS_TAGGED=1
    ok "Tailscale conectado como $(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["HostName"])')"
fi

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
echo "    2. Sin red WiFi guardada (o wlan0 10 min sin red) → AP '$AP_SSID' para configurar WiFi"
echo "    3. Con internet → Tailscale conecta solo"
echo ""
if [[ "$TS_TAGGED" != 1 ]]; then
echo "  PASO MANUAL (una sola vez antes de desplegar):"
echo "    Generá una auth key (admin de Tailscale → Settings → Keys): un solo uso,"
echo "    no efímera, tag:bench. Después:"
echo "      sudo tailscale up --auth-key=<clave> --advertise-tags=tag:bench"
echo "    Sin tag, el bench queda a nombre de tu usuario y con tus permisos en la"
echo "    tailnet: si alguien se lo lleva, entra como vos (docs/security.md)."
echo "    Reboots futuros reconectan solos."
echo ""
fi
echo "  Reiniciar para aplicar todos los cambios:"
echo "    sudo reboot"
echo "========================================================"

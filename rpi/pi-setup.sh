#!/bin/bash
# pi-setup.sh — Raspberry Pi OS hardening for espbench remote.
#
# Cubre:
#   - Sin desktop (multi-user.target)
#   - Serial TTL deshabilitado
#   - Teclado/mouse bloqueados (udev HID)
#   - WiFi provisioning con AP fallback (wifi-connect)
#   - VPN WireGuard siempre activa
#   - Servicios innecesarios deshabilitados
#
# Idempotente. Seguro correr más de una vez.
# Requiere root. Aplicar ANTES de install.sh o después — no importa.
# Reiniciar al terminar para que todos los cambios tomen efecto.

set -euo pipefail

# ---------------------------------------------------------------------------
# CONFIGURACIÓN — editar antes de correr
# ---------------------------------------------------------------------------

# WiFi provisioning: SSID y contraseña del AP de configuración
AP_SSID="espbench-config"
AP_PASS="sensify2024"

# WireGuard: datos del servidor (completar con los valores reales)
WG_SERVER_ENDPOINT="<IP_SERVIDOR>:51820"    # ej: 203.0.113.5:51820
WG_SERVER_PUBKEY="<PUBLIC_KEY_DEL_SERVIDOR>"
WG_CLIENT_IP="10.8.0.X/24"                  # IP de este Pi en la VPN

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info() { echo "[INFO]  $*"; }
warn() { echo "[WARN]  $*"; }
die()  { echo "[ERROR] $*" >&2; exit 1; }
ok()   { echo "[OK]    $*"; }

[[ $EUID -ne 0 ]] && die "Ejecutar como root: sudo bash pi-setup.sh"

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

# Sacar console=serial0 y console=ttyAMA0 del cmdline.txt
sed -i 's/console=serial0,[0-9]* //g'   "$BOOT_DIR/cmdline.txt"
sed -i 's/console=ttyAMA0,[0-9]* //g'  "$BOOT_DIR/cmdline.txt"
sed -i 's/console=ttyS0,[0-9]* //g'    "$BOOT_DIR/cmdline.txt"

# Deshabilitar UART en config.txt (si ya existe la línea, no duplicar)
if grep -q "^enable_uart=" "$BOOT_DIR/config.txt" 2>/dev/null; then
    sed -i 's/^enable_uart=.*/enable_uart=0/' "$BOOT_DIR/config.txt"
else
    echo "enable_uart=0" >> "$BOOT_DIR/config.txt"
fi

# Deshabilitar getty serie
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

# Migrar de dhcpcd a NetworkManager si es necesario
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

if ! command -v wifi-connect &>/dev/null; then
    WC_URL=$(curl -fsSL https://api.github.com/repos/balena-os/wifi-connect/releases/latest \
        | grep "browser_download_url.*aarch64" \
        | cut -d '"' -f 4)
    [[ -z "$WC_URL" ]] && die "No se pudo obtener URL de wifi-connect desde GitHub API"
    info "Descargando: $WC_URL"
    curl -fsSL "$WC_URL" | tar xz -C /usr/local/bin/
    chmod +x /usr/local/bin/wifi-connect
fi
ok "wifi-connect instalado: $(wifi-connect --version 2>/dev/null || echo 'ok')"

# Script de provisioning
cat > /usr/local/bin/wifi-provision.sh << EOSCRIPT
#!/bin/bash
# Esperar que NetworkManager intente conectar redes guardadas
sleep 20

# Verificar internet
if ping -c 2 -W 5 8.8.8.8 &>/dev/null; then
    exit 0  # Hay internet — nada que hacer
fi

# Sin internet: levantar AP portal de configuración
exec wifi-connect \\
    --portal-ssid "${AP_SSID}" \\
    --portal-passphrase "${AP_PASS}"
EOSCRIPT
chmod +x /usr/local/bin/wifi-provision.sh

# Servicio systemd
cat > /etc/systemd/system/wifi-provision.service << 'EOF'
[Unit]
Description=WiFi provisioning — AP fallback si no hay internet
After=NetworkManager.service
Wants=NetworkManager.service
Before=wg-quick@wg0.service

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
# 6. WireGuard VPN
# ---------------------------------------------------------------------------
info "Configurando WireGuard..."
apt-get install -y wireguard

# Generar keypair del cliente
WG_PRIVKEY=$(wg genkey)
WG_PUBKEY=$(echo "$WG_PRIVKEY" | wg pubkey)

cat > /etc/wireguard/wg0.conf << EOWG
[Interface]
PrivateKey = ${WG_PRIVKEY}
Address = ${WG_CLIENT_IP}
DNS = 1.1.1.1

# Kill-switch opcional: descomentar para bloquear todo tráfico si VPN cae.
# OJO: rompe wifi-connect portal si WireGuard no levanta.
# PostUp  = iptables -I OUTPUT ! -o wg0 -m mark ! --mark \$(wg show wg0 fwmark) -j REJECT
# PreDown = iptables -D OUTPUT ! -o wg0 -m mark ! --mark \$(wg show wg0 fwmark) -j REJECT

[Peer]
PublicKey = ${WG_SERVER_PUBKEY}
Endpoint = ${WG_SERVER_ENDPOINT}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
EOWG
chmod 600 /etc/wireguard/wg0.conf

systemctl enable wg-quick@wg0
ok "WireGuard habilitado (clave pública generada)"

# ---------------------------------------------------------------------------
# 7. Deshabilitar servicios innecesarios
# ---------------------------------------------------------------------------
info "Deshabilitando servicios innecesarios..."
for svc in bluetooth avahi-daemon triggerhappy; do
    systemctl disable "$svc" 2>/dev/null && info "  - $svc deshabilitado" || true
done
ok "Servicios innecesarios deshabilitados"

# ---------------------------------------------------------------------------
# 8. Tailscale
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
echo "  CLAVE PÚBLICA WireGuard de este Pi:"
echo "  $WG_PUBKEY"
echo ""
echo "  Registrala en el servidor WireGuard:"
echo "    wg set wg0 peer $WG_PUBKEY allowed-ips ${WG_CLIENT_IP%/*}/32"
echo ""
echo "  Config en: /etc/wireguard/wg0.conf"
echo ""
echo "  Flujo en próximo boot:"
echo "    1. NetworkManager intenta WiFi guardado"
echo "    2. Sin internet → AP '$AP_SSID' para configurar WiFi"
echo "    3. Con internet → WireGuard conecta al servidor"
echo ""
echo "  IMPORTANTE: completar /etc/wireguard/wg0.conf si aún"
echo "  tiene placeholders (<IP_SERVIDOR>, etc.) antes de reiniciar."
echo ""
echo "  Reiniciar para aplicar todos los cambios:"
echo "    sudo reboot"
echo "========================================================"

#!/bin/bash
# wifi-provision.sh — AP de configuración (wifi-connect, Balena) solo cuando wlan0 no tiene red.
#
# La Pi 4 tiene una sola radio: al levantar el AP suelta la red a la que estaba conectada, y el bench
# queda fuera de la LAN y de Tailscale. Por eso un corte de internet NO levanta el AP (antes un par de
# pings perdidos a 8.8.8.8 alcanzaba y el bench quedaba offline hasta reiniciarlo). El AP sale solo si:
#   - no hay ninguna red WiFi guardada (Pi nueva): enseguida, o
#   - wlan0 lleva GRACE_S seguidos sin estar conectada a una red (la guardada ya no existe).
#
# Lo instala rpi/pi-setup.sh en /usr/local/bin y lo corre wifi-provision.service.
# Se puede sourcear sin correr el loop (los tests usan step()).

AP_SSID="${AP_SSID:-SfyBench}"
AP_PASS="${AP_PASS:-1234567890}"
GRACE_S="${WP_GRACE_S:-600}"
POLL_S="${WP_POLL_S:-30}"
BOOT_DELAY_S="${WP_BOOT_DELAY_S:-20}"
AP_TIMEOUT_S="${WP_AP_TIMEOUT_S:-300}"

# Epoch del primer chequeo con wlan0 sin red; vacío mientras está conectada.
DOWN_SINCE=""

log() { echo "wifi-provision: $*"; }

# wlan0 asociada y con IP (NM la marca "connected"; "connecting (...)" todavía no cuenta).
wlan0_connected() {
    nmcli -t -f DEVICE,STATE device 2>/dev/null | grep -qx 'wlan0:connected'
}

# Hay al menos una red WiFi guardada que no es el AP propio.
has_known_wifi() {
    nmcli -t -f NAME,TYPE connection show 2>/dev/null \
        | grep ':802-11-wireless$' | grep -v "^${AP_SSID}" | grep -q .
}

run_portal() {
    log "levantando AP '$AP_SSID' ($1)"
    wifi-connect \
        --portal-ssid "$AP_SSID" \
        --portal-passphrase "$AP_PASS" \
        --ui-directory /opt/wifi-connect \
        --activity-timeout "$AP_TIMEOUT_S"
    log "AP cerrado; reconectando wlan0 a las redes guardadas"
    nmcli device connect wlan0 >/dev/null 2>&1 || true
}

# Un chequeo. $1: ahora (epoch, segundos).
step() {
    local now=$1
    if wlan0_connected; then
        [[ -n "$DOWN_SINCE" ]] && log "wlan0 volvió a conectarse"
        DOWN_SINCE=""
        return
    fi
    if ! has_known_wifi; then
        DOWN_SINCE=""
        run_portal "sin redes WiFi guardadas"
        return
    fi
    if [[ -z "$DOWN_SINCE" ]]; then
        DOWN_SINCE=$now
        log "wlan0 sin red; AP si sigue así ${GRACE_S}s"
    fi
    if (( now - DOWN_SINCE >= GRACE_S )); then
        DOWN_SINCE=""
        run_portal "wlan0 sin red hace ${GRACE_S}s"
    fi
}

main() {
    sleep "$BOOT_DELAY_S"
    while true; do
        step "$(date +%s)"
        sleep "$POLL_S"
    done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main
fi

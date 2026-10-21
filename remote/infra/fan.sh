#!/bin/bash
# fan.sh <config.txt> <model> — fan por temperatura en una Pi 4 (dtoverlay=gpio-fan). Lo llama
# install.sh. La Pi 4 no tiene conector de fan: el overlay prende el GPIO por encima de la
# temperatura. Solo sirve si el fan está cableado a ese GPIO (con transistor); en 5V/GND gira siempre.
#
# No pisa un gpio-fan que ya esté (puesto a mano o con raspi-config). Aplica al próximo reboot.
# ESPBENCH_FAN_GPIO (default 14, el de raspi-config) y ESPBENCH_FAN_TEMP en °C (default 70).
set -euo pipefail

CONFIG="$1"
MODEL="$2"
GPIO="${ESPBENCH_FAN_GPIO:-14}"
TEMP_C="${ESPBENCH_FAN_TEMP:-70}"

model="$(tr -d '\0' < "$MODEL" 2>/dev/null || true)"
case "$model" in
  "Raspberry Pi 4"*) ;;
  *) exit 0 ;;
esac

if grep -qE '^[[:space:]]*dtoverlay=gpio-fan' "$CONFIG"; then
  echo "[INFO]  fan: ya configurado en $CONFIG ($(grep -E '^[[:space:]]*dtoverlay=gpio-fan' "$CONFIG" | head -1))"
  exit 0
fi

# [all]: config.txt puede terminar en una sección [cm4]/[cm5] y la línea quedaría solo para esa placa.
printf '\n[all]\ndtoverlay=gpio-fan,gpiopin=%s,temp=%s\n' "$GPIO" "$((TEMP_C * 1000))" >> "$CONFIG"
echo "[WARN]  fan: gpio-fan en GPIO$GPIO a ${TEMP_C} °C agregado a $CONFIG; aplica al próximo reboot"

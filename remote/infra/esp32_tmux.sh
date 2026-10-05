#!/bin/bash
# esp32_tmux.sh /dev/<tty> — crea la sesión tmux de un device si no existe.
#
# Corre remote_esp32.py, que es quien escribe el log del device (DeviceLog,
# devices/<mac>/output.log) y publica su estado en run/<tty>.json. Ya no hay
# tmux pipe-pane: capturaba a ciegas lo que salía por la terminal.

DEV="$1"                       # /dev/ttyUSBX
NAME=$(basename "$DEV")        # ttyUSBX
NUM=${NAME#ttyUSB}             # X
BASE="${ESP_BASE:-/opt/esp}"

SESSION="esp32_$NAME"
PORT=$((5000 + NUM))
STATE="$BASE/run/$NAME.json"

if tmux has-session -t "$SESSION" 2>/dev/null; then
    # Si el proceso de esa sesión marcó el tty como desconectado, está por
    # terminar: recrearla ahora, si no el device que volvió queda sin sesión.
    if grep -q '"state": "disconnected"' "$STATE" 2>/dev/null; then
        tmux kill-session -t "$SESSION" 2>/dev/null || true
    else
        exit 0
    fi
fi

# Liberar lock al iniciar nueva sesión (dispositivo reconectado)
rm -f "$BASE/locks/$NAME"

tmux new-session -d -s "$SESSION" \
  "sudo $BASE/venv/bin/python3 $BASE/server/remote_esp32.py -p $DEV -tcp $PORT --base $BASE"

#!/bin/bash

DEV="$1"                       # /dev/ttyUSBX
BASENAME=$(basename "$DEV")    # ttyUSBX
NUM=${BASENAME#ttyUSB}         # X

SESSION="esp32_$BASENAME"
PORT=$((5000 + NUM))

tmux has-session -t "$SESSION" 2>/dev/null && exit 0

# Liberar lock al iniciar nueva sesión (dispositivo reconectado)
rm -f "/opt/esp/locks/$BASENAME"

LOG_DIR="/opt/esp/logs/$BASENAME"
mkdir -p "$LOG_DIR"
# Rotar log anterior si existe
if [ -s "$LOG_DIR/output.log" ]; then
    sudo mv "$LOG_DIR/output.log" "$LOG_DIR/output_$(date +%Y%m%d_%H%M%S).log"
fi

tmux new-session -d -s "$SESSION" \
  "sudo /opt/esp/venv/bin/python3 /opt/esp/server/remote_esp32.py -p $DEV -tcp $PORT"

tmux pipe-pane -t "$SESSION" "cat >> $LOG_DIR/output.log"

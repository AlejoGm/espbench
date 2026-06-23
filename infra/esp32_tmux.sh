#!/bin/bash

DEV="$1"                       # /dev/ttyUSBX
BASENAME=$(basename "$DEV")    # ttyUSBX
NUM=${BASENAME#ttyUSB}         # X

SESSION="esp32_$BASENAME"
PORT=$((5000 + NUM))

tmux has-session -t "$SESSION" 2>/dev/null && exit 0

tmux new-session -d -s "$SESSION" \
  "sudo /opt/esp/venv/bin/python3 /opt/esp/server/remote_esp32.py -p $DEV -tcp $PORT"
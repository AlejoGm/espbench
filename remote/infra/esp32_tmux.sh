#!/bin/bash
# esp32_tmux.sh /dev/<tty> — crea la sesión tmux de un device si no existe.
#
# Nombre y puerto salen de espbench-name (única fuente de esa regla): con un
# slot mapeado en slots.conf la sesión corre sobre /dev/esp-slotK y todo lo que
# cuelga del nombre (sesión, estado runtime, lock) queda estable aunque el
# kernel renumere los ttyUSB.
#
# remote_esp32.py escribe el log del device (DeviceLog) y publica su estado en
# run/<nombre>.json. Ya no hay tmux pipe-pane.

DEV="$1"                                   # /dev/ttyUSBX (o /dev/esp-slotK)
BASE="${ESP_BASE:-/opt/esp}"
DEVDIR="${ESPBENCH_DEV_DIR:-/dev}"         # solo para tests
read -r NAME PORT < <(espbench-name "$DEV") || { echo "espbench-name falló para $DEV" >&2; exit 1; }

SESSION="esp32_$NAME"
STATE="$BASE/run/$NAME.json"
PORT_TTY="$DEVDIR/$NAME"

# udev crea /dev/esp-slotK en el mismo evento; al boot ya está. Por las dudas
# se espera un poco: sin el symlink, la sesión arrancaría y se daría por
# desconectada al instante.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -e "$PORT_TTY" ] && break
  sleep 0.2
done
if [ ! -e "$PORT_TTY" ]; then
  echo "esp32_tmux: $PORT_TTY no existe (¿regla udev de slots instalada?)" >&2
  exit 1
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
  # Si el proceso de esa sesión marcó el tty como desconectado, está por
  # terminar: recrearla ahora, si no el device que volvió queda sin sesión.
  if grep -q '"state": "disconnected"' "$STATE" 2>/dev/null; then
    tmux kill-session -t "$SESSION" 2>/dev/null || true
  else
    exit 0
  fi
fi

# Liberar el lock del flash al iniciar nueva sesión (dispositivo reconectado).
# Una reserva ("user:token:expires[:mac]", misma regla que locks.parse) se
# conserva: sobrevive un replug, y si en el puerto quedó otra placa la borra
# remote_esp32.py al arrancar (compara la MAC). Cualquier otra cosa (incluido un
# lock viejo con ':' en el token) es permanente y se borra, como siempre.
LOCK="$BASE/locks/$NAME"
if [ -f "$LOCK" ]; then
  content="$(tr -d '\n\r' < "$LOCK")"
  if ! [[ "$content" =~ ^[^:]*:[^:]*:[0-9]+(:[0-9A-Fa-f]{12})?$ ]]; then
    rm -f "$LOCK"
  fi
fi

CMD="sudo $BASE/venv/bin/python3 $BASE/server/remote_esp32.py -p $PORT_TTY -tcp $PORT --base $BASE"

# Sin tmux server corriendo, este new-session lo crea, y el server (con todas las
# sesiones que vengan después) queda en el cgroup de quien llamó. Si es un unit de
# systemd que al terminar mata su cgroup, mueren todas las placas: le pasó al update
# (`devremote --reset` dentro del systemd-run de POST /api/update: el reset deja sin
# sesiones, el server sale, el nuevo nace en el unit del update y systemd lo mata al
# terminar el update) y le pasa al api (`devremote-reset` desde dashboard.service:
# el próximo restart del dashboard se lleva las sesiones). En un scope propio el
# server vive hasta que se cierre su última sesión, lo haya lanzado quien sea.
if ! tmux ls >/dev/null 2>&1 && command -v systemd-run >/dev/null 2>&1; then
  if sudo -n systemd-run --scope --quiet --collect --unit="espbench-tmux-${NAME}-$$" \
       --uid="$(id -u)" --gid="$(id -g)" -- tmux new-session -d -s "$SESSION" "$CMD"; then
    exit 0
  fi
  echo "esp32_tmux: systemd-run --scope falló: el tmux server queda en el cgroup de quien llamó" >&2
fi
tmux new-session -d -s "$SESSION" "$CMD"

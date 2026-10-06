# espbench — Root

Flasheo remoto de ESP32 + monitor serie persistente. Cliente Python en la máquina del developer, server Python en una Raspberry Pi con los ESP32 enchufados, dashboard web.

Arquitectura completa: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Leerlo antes de tocar `remote/`.

## Estructura

```
espbench/
├── common.py          # Compartido cliente/server: framing TCP, SHA256, MAC↔SN
├── client/            # Máquina del developer: deploy.py (humanos), CLI espbench + espbench_lib (agentes), agent/SKILL.md
├── remote/            # Raspberry Pi
│   ├── server/        # Un proceso por device (remote_esp32.py) + backend del dashboard (api.py)
│   ├── dashboard/     # Frontend del dashboard (HTML/CSS/JS, sin build)
│   └── infra/         # devremote, esp32_tmux.sh, espbench-name, udev, systemd
├── tests/             # pytest, corre en host sin hardware
├── rpi/               # Setup de una Pi nueva
├── docs/              # ARCHITECTURE.md, PI_CHECKLIST.md (lo que se prueba en la Pi); specs/ = diseño histórico; archive/ = PRDs y código viejo
└── issues/            # Histórico (01-13). Los issues nuevos van a GitHub Issues.
```

## Ideas clave

- **Un proceso `remote_esp32.py` por device**, cada uno en su sesión tmux. No se consolida en un servicio único: ver la decisión en `docs/ARCHITECTURE.md` §1.
- **Modelo**: `TtyPort` (puerto físico) + `Device` (identidad por MAC, FSM: DISCOVERING → MONITORING ⇄ FLASHING/ERASING, UNKNOWN, DISCONNECTED). Transición inválida = `InvalidTransition`, no se asume que "nunca pasa".
- **Estado entre procesos**: `/opt/esp/run/<tty>.json` (lo escribe el device, lo lee el dashboard). Los datos persistentes van en `/opt/esp/devices/<mac>/`.
- **Logs**: `taglog.info(TAG, msg)` con un TAG por módulo. Nada de `print`/`nprint`.
- **Nombres y puertos**: la regla vive en un solo lugar, `remote/infra/espbench-name` (`ttyUSBN` → 5000+N, o `esp-slotK` si el puerto físico está en `slots.conf`). El código Python no deriva puertos: los recibe por `--control-port`.
- **Rutas**: todo pasa por `remote/server/paths.py`; nunca escribir `"/opt/esp"` a mano.
- **Imports del server**: siempre `from server import X` / `from server.X import Y`. Si se importa `monitor` suelto y también `server.monitor`, Python carga dos módulos distintos (con dos copias de su estado).

## Tests

```bash
pytest tests/
```

Corren en el host, sin hardware. `tests/conftest.py` apunta `ESP_BASE` a un directorio temporal, así que ningún test toca `/opt/esp`. Lo que solo se puede verificar en la Pi está en `docs/PI_CHECKLIST.md` (P0–P3, desde `docs/ARCHITECTURE.md` §10).

## Server en la Pi

```bash
sudo bash remote/install.sh        # instalación (idempotente)
sudo bash remote/infra/update.sh   # actualizar: pull + install + restart
```

## Versioning

`VERSION` en la raíz. En cada commit, bump de semver (patch para fixes, minor para features, major para breaking) y actualizar la referencia en `README.md`.

## Git

Nunca agregar `Co-Authored-By: Claude` a los commits de este repo.

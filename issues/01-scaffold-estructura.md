# #01 — Scaffold: estructura de carpetas + limpieza

## What to build

Reorganizar el repo a la estructura objetivo sin cambios funcionales. Al final del slice, el servidor sigue funcionando igual que antes — solo cambia dónde viven los archivos.

Acciones:
- Crear carpetas `server/`, `client/`
- Mover `remote_esp32(server).py` → `server/remote_esp32.py`
- Mover `deploy(client).py` → `client/deploy.py`
- Borrar `devremote(server).sh` (era copia incorrecta de deploy)
- `infra/` ya existe con los archivos correctos — no mover
- Crear `requirements.txt` con `esptool` y `esp-idf-monitor`
- Actualizar `infra/devremote` y `infra/esp32_tmux.sh` para apuntar a la nueva ruta `server/remote_esp32.py` si tienen rutas hardcodeadas

## Acceptance criteria

- [ ] `python server/remote_esp32.py --help` funciona sin errores
- [ ] `python client/deploy.py --help` funciona sin errores
- [ ] No existe `devremote(server).sh` ni `flashd(server).py` en raíz
- [ ] `requirements.txt` existe con `esptool` y `esp-idf-monitor`
- [ ] `infra/devremote` referencia la ruta correcta al servidor

## Blocked by

None — puede empezar inmediatamente.

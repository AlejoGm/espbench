# remote/dashboard/

Frontend del dashboard. Lo sirve `server/api.py` (montaje estático de FastAPI). Vanilla HTML/CSS/JS: sin framework, sin build.

| Archivo | |
|---|---|
| `index.html` | Grilla de cards, una por device. Pollea `/api/devices` cada 5 s |
| `device.html` | Log del device en vivo (`/ws/device/{tty}`) + badges + botones reset/boot/sesión/unlock |
| `style.css` | Tema oscuro, grilla responsive |

## index.html

- Card por device: nombre (renombrable, `PATCH /api/devices/{mac}`), modelo de HW, versión de firmware, IDF, último deployer, SN, tty, último flash, puerto TCP, lock.
- Dos badges de estado:
  - `status` (RUNNING/DOWN): si hay un proceso vivo atendiendo el device.
  - `state`, el estado de la FSM: **FLASHEANDO** / **BORRANDO** / **INICIANDO** / **SIN MAC** / **DESCONECTADO**. `monitoring` es lo normal y no lleva badge.
- Los devices con MAC conocida van en la grilla principal; los que no, en "sin identificar".
- `tty_name` puede ser `ttyUSBN` o `esp-slotK` (slots, ver `remote/infra/`).

## device.html

- `?tty=<nombre>`. WebSocket a `/ws/device/{tty}`: manda la sesión actual completa y después el stream en vivo. Si la sesión rota, se ve el arranque de la nueva.
- El log mezcla el serial del ESP32 con las líneas de `taglog` del server (flash, esptool, transiciones).
- Los badges (`/api/device/{tty}`) se refrescan cada 3 s, así se ve el estado `flashing` mientras dura.

## Notas

- Las llamadas usan el mismo host/puerto que la página (no hay URLs hardcodeadas).
- No tiene autenticación: es solo para uso en la red interna.
- Para probarlo sin Pi: levantar `server.api` con devices simulados en `run/<tty>.json` (necesita `uvicorn[standard]`, que es el que trae soporte de WebSocket).

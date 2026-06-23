# #10 — `server/device_registry.py` — detección de dispositivos + estado

## What to build

Módulo que centraliza el estado de los dispositivos conectados. Sin acceso a hardware — solo filesystem y subprocess.

Funcionalidad:
- `list_devices()` → lista de `DeviceInfo` por cada `/dev/ttyUSBX` presente
- `DeviceInfo`: tty, port_tcp (5000+X), status (RUNNING/DOWN), last_flash_ts (timestamp del job más reciente en `/opt/esp/jobs/`), chip_id (None hasta que se detecte)
- Estado RUNNING/DOWN: `tmux has-session -t esp32_ttyUSBX` (subprocess)
- `set_chip_id(tty, chip_id)` — el LogStreamer llama a esto cuando parsea `CHIPID = \d+` del log
- `GET /api/devices` y `GET /api/device/{tty}` en `dashboard.py` usan este módulo

## Tests (`tests/test_device_registry.py`)

- `list_devices()` detecta dispositivos en un `/dev` simulado (tmpdir con archivos `ttyUSB0`, `ttyUSB1`)
- `set_chip_id` persiste el chip_id y `get_device` lo retorna
- `last_flash_ts` se obtiene del directorio de jobs (tmpdir con estructura real)
- Estado DOWN cuando tmux subprocess retorna código != 0 (mock)

## Acceptance criteria

- [ ] `list_devices()` retorna un entry por cada `ttyUSBX` en `/dev`
- [ ] `status` es RUNNING si la sesión tmux existe, DOWN si no
- [ ] `last_flash_ts` es el timestamp del job más reciente para ese dispositivo
- [ ] `chip_id` es None hasta que `set_chip_id` es llamado, luego retorna el valor
- [ ] Todos los tests pasan en host sin hardware

## Blocked by

#09

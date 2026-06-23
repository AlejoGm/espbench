# #08 — Actualizar `ARCHITECTURE.md` al estado final

## What to build

Reescribir `ARCHITECTURE.md` para que refleje el sistema tal como quedó después de implementar #01–#07. El documento actual describe el estado pre-reestructura con su deuda técnica — ya no es válido.

Secciones a actualizar:
- Diagrama de archivos (estructura de carpetas final)
- Descripción de cada módulo (`common.py`, `server/flash.py`, `server/monitor.py`, `server/protocol.py`)
- Flujo del pipeline `.elf` (cliente → ZIP → servidor → `current.elf` → monitor)
- Sección de infraestructura: `devremote`, `esp32_tmux.sh`, udev, systemd — con sus ubicaciones en la Pi
- Instrucciones de install (`./install.sh`)
- Dependencias externas actualizadas (`esp-idf-monitor` reemplaza `picocom`)
- Eliminar la sección "Deuda técnica" del estado anterior (ya resuelta)
- Agregar sección "Norte futuro" con el dashboard web HTTP+WebSocket

## Acceptance criteria

- [ ] `ARCHITECTURE.md` no menciona `picocom`, `flashd`, ni `PicocomMonitor`
- [ ] El diagrama de carpetas coincide con la estructura real del repo
- [ ] Existe sección de "Cómo instalar en la Pi" referenciando `install.sh`
- [ ] Existe sección "Norte futuro" con dashboard web

## Blocked by

- #01, #02, #03, #04, #05, #06, #07

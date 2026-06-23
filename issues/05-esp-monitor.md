# #05 — `server/monitor.py` — EspMonitor reemplaza PicocomMonitor

## What to build

Crear `server/monitor.py` con `EspMonitor`, que reemplaza `PicocomMonitor` de `remote_esp32.py`. Misma interfaz pública, distinto proceso interno.

Interfaz (sin cambios para el resto del sistema):
- `__init__(tty_path, baud, logs_dir, elf_path=None, cfg=None, svc_log=None)`
- `start()`
- `stop()`
- `restart()`
- `get_recent_output() -> str`

Cambios internos:
- Reemplaza `picocom` por `esp-idf-monitor` (`idf_monitor` del paquete `esp-idf-monitor`)
- `elf_path: Path | None` — si es None o no existe en disco, arranca sin decodificación (fallback silencioso, log de advertencia)
- Buffer circular de 64KB se mantiene (usado por `erase_region_interactive`)
- Ctrl-E → `erase_region_interactive` se mantiene sin cambios
- Ctrl-C → termina servidor, sin cambios

`erase_region_interactive` y `parse_partition_table` se mueven también a `monitor.py`.

El servidor instancia `EspMonitor` con `elf_path=base/"current.elf"` — ese archivo no existe hasta el primer flash, lo cual activa el fallback silencioso automáticamente.

## Acceptance criteria

- [ ] `server/monitor.py` existe con `EspMonitor`
- [ ] `PicocomMonitor` eliminado de `remote_esp32.py`
- [ ] Con `elf_path=None`, el monitor arranca sin errores (sin decodificación)
- [ ] Con `elf_path` válido, `esp-idf-monitor` decodifica backtraces en la salida
- [ ] Ctrl-E sigue activando erase region interactivo
- [ ] `picocom` ya no es dependencia (eliminado de `requirements.txt` si estaba)

## Blocked by

- #01

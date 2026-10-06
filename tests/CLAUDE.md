# tests/

pytest. Corre en el host (Mac/Linux), sin Pi ni hardware.

```bash
pytest tests/
```

`conftest.py` apunta `ESP_BASE` a un directorio temporal en **todos** los tests, así ninguno toca el `/opt/esp` real (en la Pi leerían el estado y los logs de los devices de verdad).

| Archivo | Qué cubre |
|---|---|
| `test_device.py` | FSM de `Device`, `TtyPort`, `DeviceManager` (reintentos de MAC, MAC por serial, watcher del tty), publicación de estado, eventos `state` y del serial apuntando a su línea |
| `test_serial_watch.py` | `SerialWatch.on_line`: resets, panics, boot loop, firmware, `\r`/ANSI, chunks por la tubería real; eventos `boot`/`panic`/`fw`/`boot_loop` |
| `test_device_log.py` | `DeviceLog`: prefijo, chunks y UTF-8 partidos, parcial retenido y `↪`, taglog en medio de una serial, header fuera del buffer, offsets en bytes, rotación por `session_id`, buffer pre-MAC, migración; eventos con cursor exacto (pre-MAC, migración solo de la sesión) |
| `test_events.py` | `events.jsonl`: dos procesos escribiendo a la vez, lectura, truncado, migración de sesión, cursor del fin del log, `record()` del api |
| `test_logrange.py` | Rangos del log: anchors (ordinales, tiempo con horas desordenadas y líneas sin prefijo, cursor a mitad de línea, vencido), `until` (evento, boot/panic en las líneas, patrón en la línea lógica, eco, histórico vs espera), `around`, filtros, truncado, `/events` |
| `test_locks.py` | `locks/<tty>`: formato con y sin vencimiento, vencido se ignora (no se borra al leer), flock, reserva de otra placa |
| `test_runstate.py` | `run/<tty>.json`: escritura atómica, lectura, `pid_alive` |
| `test_protocol.py` | Pedido de flash completo por `socketpair`, esptool falso: auth (también por `api_token`), lock y reservas, SHA256, retry sin `--encrypt`, device cambiado, FSM, evento `flash`; el done llega con el monitor relanzado, la FSM en `monitoring` y el `cursor` del flash |
| `test_remote_esp32.py` | El entrypoint entero con fakes solo en esptool/monitor/TCP: arranque, señal ignorada durante flash, desconexión, MAC por serial |
| `test_erase.py` | Modo Erase Region con un monitor falso que solo tiene la interfaz pública; `EspMonitor` (sink, elf) |
| `test_partition_table.py` | Parseo de la tabla de particiones del bootloader |
| `test_device_registry.py` | Vista del dashboard: estado runtime, slots, último flasheo, `devices.json` sin corrupción concurrente |
| `test_history.py` | Historial: jobs con/sin `result.json`, sesiones, path traversal |
| `test_api.py` | Endpoints llamando los handlers directo (no hay httpx): historial, `send` (cursor previo, evento, `expect_mac`, 409 ocupado), reservas (`reserve`/`release`, 423 a otros, `force`), `command` con tty validado, token en las escrituras, `/api/board/{key}/log|events` por key/SN/MAC con la placa desconectada, orden de rutas |
| `test_log_streamer.py` | WebSocket: contenido inicial, stream, rotación, `log_path` desde el estado runtime |
| `test_dashboard_js.py` | Corre `tests/js/test_*.js` con `node --test` (lógica del frontend en `espbench.js`); se saltea sin node |
| `test_infra.py` | Scripts bash reales (`espbench-name`, `esp32_tmux.sh`, `devremote`) con `tmux`/`udevadm`/`pkill` falsos; `devremote.service` espera a `time-sync.target` |
| `test_flash.py`, `test_common.py`, `test_artifact.py`, `test_paths.py`, `test_taglog.py` | Utilidades |

## Criterio

- Cada bug que se arregla viene con un test que **falla con el código anterior**. Hay que comprobarlo: un test que pasa en los dos casos no prueba nada.
- Los fakes van solo en los bordes (esptool, PTY, socket, tmux). El modelo se usa real.
- Lo que no se puede probar acá (udev, systemd, esptool/monitor contra hardware) está en `docs/ARCHITECTURE.md` §10.

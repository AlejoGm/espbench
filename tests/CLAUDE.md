# tests/

pytest. Corre en el host (Mac/Linux), sin Pi ni hardware.

```bash
pytest tests/
```

`conftest.py` apunta `ESP_BASE` a un directorio temporal en **todos** los tests, así ninguno toca el `/opt/esp` real (en la Pi leerían el estado y los logs de los devices de verdad).

| Archivo | Qué cubre |
|---|---|
| `test_device.py` | FSM de `Device`, `TtyPort`, `DeviceManager` (reintentos de MAC, MAC por serial, watcher del tty), publicación de estado |
| `test_device_log.py` | `DeviceLog`: buffer, rotación por sesión, hogar provisorio sin MAC, migración, UTF-8 partido |
| `test_runstate.py` | `run/<tty>.json`: escritura atómica, lectura, `pid_alive` |
| `test_protocol.py` | Pedido de flash completo por `socketpair`, esptool falso: auth, lock, SHA256, retry sin `--encrypt`, device cambiado, FSM |
| `test_remote_esp32.py` | El entrypoint entero con fakes solo en esptool/monitor/TCP: arranque, señal ignorada durante flash, desconexión, MAC por serial |
| `test_erase.py` | Modo Erase Region con un monitor falso que solo tiene la interfaz pública; `EspMonitor` (sink, elf) |
| `test_partition_table.py` | Parseo de la tabla de particiones del bootloader |
| `test_device_registry.py` | Vista del dashboard: estado runtime, slots, último flasheo, `devices.json` sin corrupción concurrente |
| `test_log_streamer.py` | WebSocket: contenido inicial, stream, rotación, `log_path` desde el estado runtime |
| `test_infra.py` | Scripts bash reales (`espbench-name`, `esp32_tmux.sh`, `devremote`) con `tmux`/`udevadm`/`pkill` falsos |
| `test_flash.py`, `test_common.py`, `test_artifact.py`, `test_paths.py`, `test_taglog.py` | Utilidades |

## Criterio

- Cada bug que se arregla viene con un test que **falla con el código anterior**. Hay que comprobarlo: un test que pasa en los dos casos no prueba nada.
- Los fakes van solo en los bordes (esptool, PTY, socket, tmux). El modelo se usa real.
- Lo que no se puede probar acá (udev, systemd, esptool/monitor contra hardware) está en `docs/ARCHITECTURE.md` §10.

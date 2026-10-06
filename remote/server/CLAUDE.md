# remote/server/

Código Python que corre en la Pi. Hay dos tipos de proceso: **uno por device** (`remote_esp32.py`, en tmux) y **uno de dashboard** (`api.py`, systemd). Se comunican solo por disco (`run/<tty>.json`, `devices.json`, logs). Arquitectura: [../../docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md).

## Archivos

| Archivo | Proceso | Qué hace |
|---|---|---|
| `remote_esp32.py` | device | Entrypoint: arma `DeviceManager`, identifica por MAC, levanta monitor + control server + fallback de MAC por serial + watcher del tty |
| `device.py` | device | `TtyPort` / `Device` (FSM) / `DeviceManager` (`on_keys`: un Ctrl-T Ctrl-R/P al monitor pone en cero los contadores de `SerialWatch`) |
| `serial_watch.py` | device | `SerialWatch.on_line`: detecta resets, panics, boot loop (5 boots en 60 s; en el loop los panics van en el `end`) y versión de firmware en las líneas que le pasa `DeviceLog` → `health`/`fw` en `run/<tty>.json` y eventos |
| `device_log.py` | device | `DeviceLog`: único escritor del log del device (`devices/<mac>/output.log`): líneas con prefijo de hora y origen, sesión, cursores en bytes, eventos con el cursor exacto |
| `events.py` | ambos | `events.jsonl` por placa (append atómico entre procesos, lectura, migración de sesión), cursores `c:<sesión>:<offset>`, `record()` para el api |
| `monitor.py` | device | `EspMonitor`: `esp_idf_monitor` en un PTY; serial → stdout + `DeviceLog`; teclado → `input_sink` (`DeviceManager.on_keys`) → monitor; Ctrl-C / Ctrl-E |
| `protocol.py` | device | Servidor TCP de flasheo, partido en fases (`authenticate`, `LockStore`, `receive_artifact`, `run_flash`...) |
| `erase.py` | device | Modo Erase Region (Ctrl-E) |
| `partition_table.py` | device | Parseo de la tabla de particiones que imprime el bootloader |
| `flash.py` | device | esptool: buscarlo, armar comandos, correrlos (`run_cmd`), leer MAC |
| `logrange.py` | dashboard | Rangos del log de una placa para `/api/board/{key}/log` y `/events`: anchors, `until`, `around`, filtros, truncado. Solo lee disco; `events.jsonl` una vez por pedido y de atrás para adelante |
| `api.py` | dashboard | FastAPI: REST + WebSocket + estáticos. Antes `dashboard.py` |
| `device_registry.py` | dashboard (+ device) | `DeviceRegistry` (vista de lectura de los devices), `DevicesFile` (`devices.json`, con `flock`; `set_meta`: nota y propiedades) |
| `board_meta.py` | dashboard | Nota y propiedades por placa: categorías fijas (`CATEGORIES`, editar acá), valores por bench (`meta/properties.json`: `meta/` es escribible por el api, `/opt/esp` no), validación con el más parecido |
| `history.py` | dashboard | Historial por device: jobs (`result.json`, `job.log`) y sesiones de log rotadas; valida nombres que llegan por URL |
| `log_streamer.py` | dashboard | Tail del log de cada device → WebSocket (solo transmite: el firmware lo publica `SerialWatch`) |
| `auth.py` | ambos | Token opcional `/opt/esp/api_token`: escrituras del API (`Bearer`) y token del flash si no hay `--token` |
| `locks.py` | ambos | `locks/<tty>`: `user:token[:expires[:mac]]` (lock del flash / reserva; `:` y `%` del par escapados); vencido = inexistente: **se ignora, no se borra al leerlo**; `python3 -m server.locks unlock <tty>` (lo usa `devremote --unlock`) |
| `runstate.py` | ambos | `run/<tty>.json`: escritura atómica, lectura, `pid_alive` |
| `update.py` | dashboard (+ `espbench-update`) | `busy_reason` (placa flasheando o reservada), PIN y estado del último update, comando para lanzarlo |
| `paths.py` | ambos | Todas las rutas bajo `ESP_BASE` (default `/opt/esp`) |
| `taglog.py` | ambos | Logging `taglog.info(TAG, msg)`, sinks pluggables |

## Reglas

- **Rutas**: siempre `paths.*()`, nunca `"/opt/esp"` a mano. `ESP_BASE` se lee en cada llamada, así los tests la pisan con `monkeypatch.setenv`.
- **Logs**: `taglog` con un `TAG = "<modulo>"` por archivo. Nada de `print`.
- **Imports**: `from server import X` / `from server.X import Y`. Nunca `from monitor import ...` suelto: Python lo carga como un módulo distinto de `server.monitor`, y `taglog` (que tiene estado: la lista de sinks) quedaría duplicado.
- **Estado del device**: solo a través de la FSM (`start_flash`, `promote`...). Si una transición no se permite, `InvalidTransition`. Nada de flags sueltos.
- **Puertos**: no derivarlos. `remote_esp32.py` los recibe por `--control-port` (los decide `infra/espbench-name`). `TtyPort.from_tty_path` y `DeviceRegistry._parse_tty_number` existen solo como fallback y para tests.
- **Datos por device**: si `device.mac` está, en `devices/<mac>/` (jobs, `current.elf`, `last_user`). Si no, en las rutas por tty. El lock va siempre por tty (a propósito, ver ARCHITECTURE §5).
- **Escrituras compartidas entre procesos**: atómicas (`runstate.write`) o con `flock` + `flush` + `fsync` **antes** de soltar el lock (`DevicesFile._update`). Sin eso, `devices.json` ya se corrompió una vez.
- **Log del device**: todo lo que va a `output.log` pasa por `DeviceLog` (`write_serial` / `write_taglog`), que pone el prefijo `YYYY-MM-DD HH:MM:SS.mmm <origen> `. Nada escribe el archivo por otro lado: los cursores son offsets en bytes y dependen de eso. Formato y eventos: ARCHITECTURE §4.
- **Locks entre `DeviceLog` y el resto**: `DeviceLog` nunca llama hacia afuera con su lock tomado (`line_sink` se llama después de soltarlo). Si no, `SerialWatch` → `Device.publish` y `Device._set_state` → taglog → `DeviceLog` se bloquean entre sí.
- Python 3.9 en la Pi: nada de `X | None` en firmas de función ni en anotaciones a nivel módulo (`Optional[X]`).

## Tests

Cada módulo tiene su `tests/test_<modulo>.py`. Los que importan:

- `test_protocol.py`: pedido completo por `socketpair` con esptool falso.
- `test_remote_esp32.py`: el entrypoint entero con fakes solo en los bordes.

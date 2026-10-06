# tests/

pytest. Corre en el host (Mac/Linux), sin Pi ni hardware.

```bash
pytest tests/
```

`conftest.py` apunta `ESP_BASE` a un directorio temporal en **todos** los tests, así ninguno toca el `/opt/esp` real (en la Pi leerían el estado y los logs de los devices de verdad).

| Archivo | Qué cubre |
|---|---|
| `test_device.py` | FSM de `Device`, `TtyPort`, `DeviceManager` (reintentos de MAC, MAC por serial, watcher del tty), publicación de estado, eventos `state` y del serial apuntando a su línea |
| `test_serial_watch.py` | `SerialWatch.on_line`: resets, panics, boot loop (umbral 5 en 60 s, panics agregados en el `end`), firmware, `\r`/ANSI, chunks por la tubería real; eventos `boot`/`panic`/`fw`/`boot_loop` |
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
| `test_board_meta.py` | Propiedades: categorías iniciales, alta/baja de valores (en uso, inexistente), archivo roto, plan/apply de cambios; nota (validación); `DevicesFile.set_meta` (también desde varios procesos) |
| `test_history.py` | Historial: jobs con/sin `result.json`, sesiones, path traversal |
| `test_api.py` | Endpoints llamando los handlers directo (no hay httpx): historial, `send` (cursor previo, evento, `expect_mac`, 409 ocupado), reservas (`reserve`/`release`, 423 a otros, `force`; `unlock` con `force: true` y evento `release`), `command` con tty validado, token en las escrituras, `/api/board/{key}/log|events` por key/SN/MAC con la placa desconectada, orden de rutas, nota y propiedades (`PATCH /api/devices/{mac}`: validación, eventos `note`/`props`, user por defecto = host, token) y `/api/properties` (alta, baja, en uso) |
| `test_log_streamer.py` | WebSocket: contenido inicial, stream, rotación, `log_path` desde el estado runtime; no parsea ni lee logs al arrancar |
| `test_linemark_parity.py` | `EB.lineMark` (JS) y `serial_watch.line_kind` (Python) dan lo mismo sobre los mismos casos (corre node); se saltea sin node |
| `test_contract_parity.py` | Los otros contratos escritos dos veces: prefijo de línea (`logrange` ↔ `EB.splitPrefix`), cursor (`events` ↔ `EB.parseCursor` ↔ `espbench_lib`), nombre de sesión (`make_session_id` ↔ `history.SESSION_RE` ↔ `EB.sessionStart`) |
| `test_dashboard_js.py` | Corre `tests/js/test_*.js` con `node --test` en tres zonas horarias (UTC, Argentina, Tokio) (lógica del frontend en `espbench.js`: prefijo, clases de línea, reservas y "vence en", filas y contexto de eventos, marcas del vivo); se saltea sin node |
| `test_geo.py` | Ubicación del bench con HTTP falso (nada sale a la red; `conftest` pone `ESPBENCH_GEO=off` para el resto): ipinfo primero con timeout corto, fallback a ipapi.co (error, JSON roto, rate limit, IP privada), falla → lo último `stale`, override manual, desactivada, archivos en `meta/` con `ESP_BASE` de solo lectura, thread (consulta al arrancar, espera 24 h; tras un fallo, 1 h) |
| `test_benchinfo.py` | Salud de la máquina con `/proc` y `/sys` falsos (y sin ellos), actividad por hora (ventana, boot loop solo el inicio, recientes), `last_log_epoch`, endpoints |
| `test_update.py` | `espbench-update` con git real (origin bare con tags y ramas) e install/systemctl/curl falsos: último release por versión, PIN (ref, seguir la rama, `--release`), ocupado (`--auto` saltea, manual `--force`), rollback, up_to_date, lock |
| `test_infra.py` | Scripts bash reales (`espbench-name`, `esp32_tmux.sh`, `devremote`, `pip-deps.sh`) con `tmux`/`udevadm`/`pkill`/`pip` falsos; `devremote.service` espera a `time-sync.target`; `devremote --unlock` con el server de verdad (flock + evento); `regex` opcional en el install |
| `test_master.py` | bench-master (venv con `master/requirements.txt` + pytest): cache, API, proxy HTTP/WS, guard, punta a punta; nota y propiedades pasan al master y el catálogo de cada bench se lee por el proxy |
| `test_espbench_lib.py` | `client/espbench_lib.py` contra `benchsim`: config, resolve, esperas (idle, `--for`, patrón, eco en el 2º poll, línea lógica partida, timeout), panic → `crashed` (también como `↪` del prompt y con el evento tarde), `--expect-panic`, `session_ended`, errores del contrato (busy, locked, reservation_lost, token_mismatch, device_changed, auth, auth_config, not_found, bad_anchor, cursor_expired, network), flash + verify (sesión nueva, panic/reboot en la ventana, until), reset; uno con uvicorn |
| `test_espbench_cli.py` | El CLI con `--json` (`main(argv)` en el proceso; por `subprocess` el contrato, `python -m` e `install.sh`): un objeto por comando, exit codes, ciclo reserve → send → logs/events → release, flash `--verify`, argumentos validados antes de escribir, salida humana; discovery sin host (benchsim + benches falsos por HTTP, viejo ignorado, ambiguo, cache); nota, `set`, `props`, `ls --where/--free`, `pick` (`--reserve`, carrera) |
| `test_deploy.py` | `deploy.py` importa el flash de la lib y su salida no cambió; `flash_one` contra el protocolo real |
| `benchsim.py` | No es un test: la Pi simulada (API real por un adaptador `http.server` o uvicorn, `SimBoard` con `DeviceManager`/`DeviceLog` reales con el hold de la línea parcial en 30 ms, tmux y esptool falsos). También se corre a mano: `ESP_BASE=$(mktemp -d) python -m tests.benchsim` |
| `test_flash.py`, `test_common.py`, `test_artifact.py`, `test_paths.py`, `test_taglog.py` | Utilidades |

## Criterio

- Cada bug que se arregla viene con un test que **falla con el código anterior**. Hay que comprobarlo: un test que pasa en los dos casos no prueba nada.
- Los fakes van solo en los bordes (esptool, PTY, socket, tmux). El modelo se usa real.
- `benchsim` pisa `api.subprocess` con un namespace propio, nunca `subprocess.run` global (`api.subprocess` **es** el módulo `subprocess`: pisar su `.run` rompe todo subprocess del proceso).
- Lo que no se puede probar acá (udev, systemd, esptool/monitor contra hardware) está en `docs/ARCHITECTURE.md` §10.

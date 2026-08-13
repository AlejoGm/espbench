# remote/server/

Core server logic running on Raspberry Pi. One instance per device (ttyUSBN).

## Files

| File | Purpose |
|------|---------|
| `remote_esp32.py` | Entrypoint: args, logging, MAC read, monitor + TCP server startup |
| `protocol.py` | TCP control server: orchestrates flash workflow |
| `flash.py` | esptool integration: find, build command, execute |
| `monitor.py` | `esp_idf_monitor` PTY wrapper: circular buffer, log rotation |
| `device_registry.py` | Device metadata: MAC, SN, fw info, status, locking |
| `dashboard.py` | FastAPI app: REST + WebSocket APIs + static file serving |
| `paths.py` | Fuente única de rutas bajo `ESP_BASE` (env var, default `/opt/esp`) — nadie más debe hardcodear `/opt/esp` |
| `device_log.py` | `DeviceLog`: bufferea el log de un device hasta conocer su MAC, después escribe a `paths.device_output_log(mac)`. Todavía no conectado a `EspMonitor`/`protocol.py` |

## Rutas (`paths.py`)

Todo path generado en runtime (`logs/`, `jobs/`, `locks/`, `devices.json`, `current_<tty>.elf`, etc.) sale de `paths.py`, nunca de un literal `"/opt/esp"` repetido. Base configurable con la env var `ESP_BASE` (`remote_esp32.py --base` la setea al arrancar).

`paths.py` también expone el esquema nuevo por-device (`device_home(mac)`, `device_output_log(mac)`, ...), keyed por MAC en vez de tty — todavía sin usar, es el target de la próxima fase del refactor (ver conversación de arquitectura / `DeviceLog`).

## Entrypoint (`remote_esp32.py`)

CLI args: `--port-tty`, `--serial-baud`, `--control-port`, `--chip`, `--flash-baud`, `--token`, `--base`

Startup sequence:
1. Read device MAC (`flash.read_mac()`) — port must be free
2. Register MAC in `/opt/esp/devices.json`
3. Start `EspMonitor` (background serial monitor)
4. Start `control_server` (TCP listener, daemon thread)
5. Block on signal (SIGTERM/SIGINT/SIGHUP → graceful shutdown)

## Flash Protocol (`protocol.py`)

TCP control server per-connection flow:
1. Receive header JSON (token, action, job metadata)
2. Validate token
3. Create job dir under `/opt/esp/jobs/`
4. Receive artifact (upload bytes or download from URL)
5. Verify SHA256
6. Extract ZIP
7. Copy `firmware.elf` → `/opt/esp/current.elf`
8. `_ignore_signals_flag.set()` — inhibit interruption
9. `mon.stop()` — release serial port
10. `esptool write_flash` (with retry on encryption errors)
11. Send result JSON to client
12. `mon.start()`, `_ignore_signals_flag.clear()`

Stream mode: real-time esptool output sent via `send_msg()` during flash.

Device locking: lock file `user:token` in `/opt/esp/locks/` prevents concurrent flashes.

## Serial Monitor (`monitor.py`)

`EspMonitor` class:
- Spawns `python -m esp_idf_monitor` in PTY
- Passes `--elf /opt/esp/current.elf` if file exists (backtrace decoding)
- Relay: monitor stdout → stdout + daily log file
- Circular buffer (64 KB) for firmware info parsing
- Daily log rotation: `/opt/esp/logs/YYYYMMDD/serial.log`
- Intercepts `Ctrl-C` (shutdown server) and `Ctrl-E` (erase region interactive)
- `_ignore_signals_flag` (threading.Event) shared with entrypoint — blocks SIGTERM during flash

## Flash Tool (`flash.py`)

- `find_esptool_cmd()` — PATH or `python -m esptool`
- `build_esptool_cmd()` — reads `flasher_args.json`, resolves bin paths (dict/list/glob)
- `run_cmd()` — subprocess, line-by-line output relay
- `read_mac()` — runs `esptool read_mac`
- Default offsets: 0x1000 (bootloader), 0x8000 (PT), 0xe000 (OTA), 0x10000 (app)

## Device Registry (`device_registry.py`)

`DevicesFile` — thread-safe JSON at `/opt/esp/devices.json`, fcntl-locked:
- Maps MAC → `{ device_key, hw_model }`

`DeviceInfo` dataclass fields:
- `tty`, `tty_name`, `port_tcp`, `status` (RUNNING/DOWN)
- `mac`, `sn`, `device_key`, `hw_model`
- `fw_project`, `fw_version`, `fw_idf`
- `last_flash_ts`, `last_flash_user`, `lock_user`

`DeviceRegistry`:
- `list_devices()` — scans `/dev/ttyUSB*`
- `get_device(tty_name)` — reads MAC from `/opt/esp/logs/{tty}/mac`, detects status via `tmux has-session`
- Parses firmware info (project, version, IDF) from circular buffer / log files

## Dashboard API (`dashboard.py`)

FastAPI app:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/version` | GET | Version from `/opt/esp/VERSION` |
| `/api/devices` | GET | List all connected devices |
| `/api/device/by-key/{key}` | GET | Lookup by friendly name |
| `/api/device/{tty}` | GET | Details for one tty |
| `/api/devices/{mac}` | PATCH | Rename device (update `device_key`) |
| `/api/device/{tty}/unlock` | POST | Unlock locked device |
| `/api/device/{tty}/command/{cmd}` | POST | Send tmux key combo (reset, bootloader) |
| `/ws/device/{tty}` | WebSocket | Real-time serial log stream |

Serves static files from `../dashboard/`.

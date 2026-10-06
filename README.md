# espbench

Remote ESP32 firmware deployment system. Build on your dev machine, flash to an ESP32 connected to a Raspberry Pi over TCP. Includes a persistent serial monitor and web dashboard.

**Version:** 0.30.6

---

## Architecture

```
Dev machine                          Raspberry Pi
────────────                         ────────────────────────────────────────────
client/deploy.py ──── TCP ────►  remote_esp32.py   (one process per device, in tmux)
  │                                 ├─ Device (FSM) + DeviceLog
  │  1. idf.py build (optional)     ├─ EspMonitor (esp_idf_monitor + ELF backtraces)
  │  2. zip firmware artifact       └─ control server (port 5000+N)
  │  3. send over TCP                       │ writes devices/<mac>/, run/<tty>.json
  │  4. receive result                      ▼
  └─ .flashcfg.json               api.py (dashboard, FastAPI, port 8080)
     (local gitignored config)      └─ /api/devices, /ws/device/{tty}
```

One tmux session per device. Device name and TCP port come from `espbench-name`: `ttyUSBN` → `5000+N`, or a stable `esp-slotK` → `5000+K` if the physical USB port is mapped (see [Stable ports](#stable-ports-optional)). Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Fresh Pi Setup

Complete steps for a new Raspberry Pi.

### 1. Flash OS

Raspberry Pi OS Lite 64-bit (Bookworm). Enable SSH and set hostname via Raspberry Pi Imager advanced settings.

### 2. First SSH — install base packages

```bash
sudo apt update && sudo apt install -y git tmux python3 python3-pip python3-venv
```

### 3. Clone repo

```bash
git clone https://github.com/AlejoGm/espbench.git
cd espbench
```

### 4. System hardening (optional but recommended)

Disables desktop, serial TTL, HID, installs WiFi provisioning AP fallback and Tailscale:

```bash
sudo bash rpi/pi-setup.sh
```

After it finishes, authenticate Tailscale (one time):

```bash
sudo tailscale up    # open the printed URL in a browser
```

Then reboot to apply all changes:

```bash
sudo reboot
```

### 5. Install server

```bash
sudo bash remote/install.sh
```

Installs server files to `/opt/esp/`, creates Python venv, registers udev rules and systemd services.

### 6. Start services

```bash
sudo systemctl start devremote dashboard
```

`devremote` manages tmux sessions per device. `dashboard` exposes the web UI on port 8080.
Plug in an ESP32 via USB — udev auto-creates a session for it.

### 7. Verify

```bash
devremote --status
```

Dashboard: `http://<pi-hostname>:8080`

---

## Update (existing Pi)

```bash
sudo bash remote/infra/update.sh
```

Hace `fetch` + `pull` (aborta si hay cambios locales sin commitear), reinstala, reinicia `dashboard` y resetea las sesiones de `devremote` (necesario para que los devices ya conectados corran el código nuevo — reiniciar el servicio `devremote` solo arranca sesiones que falten, no las existentes).

---

## Quick Start

### Client (Dev machine)

```bash
cd client && ./install.sh
```

Create `.flashcfg.json` in your ESP-IDF project root. The easiest way: open the dashboard, click **⎘ Config** on a device card, and paste the copied snippet into your config file, then add the missing fields:

```json
{
  "mode": "auto",
  "paths": { "project_root": ".", "idf_py": "idf.py" },
  "remote": [
    {
      "name": "mi-board",
      "host": "sensipi03",
      "token": "",
      "lock_user": "yourname",
      "lock_token": "your_lock_token"
    }
  ],
  "chip": "esp32",
  "flash_baud": 921600
}
```

Flash:

```bash
python client/deploy.py
```

---

## Uso por agentes (`espbench`)

CLI para que un agente (Claude Code) cierre el ciclo contra una placa real: flash, verificar el arranque, mandar
comandos por la consola serie, esperar la respuesta, leer log y eventos. Salida `--json` (un objeto por comando) y
exit codes por causa. Spec: [docs/specs/agents-cli.md](docs/specs/agents-cli.md) §8.

```bash
cd client && ./install.sh          # deja `espbench` en ~/.local/bin (ESPBENCH_BIN_DIR para otro lugar)

espbench ls --json
espbench reserve mi-board --ttl 30m --json
idf.py build && espbench flash mi-board --verify --json        # flash + primer boot + 10 s sin crash
espbench send mi-board "status" --until "OK" --json
espbench events mi-board --type panic --json
espbench logs mi-board --around panic --max-lines 80 --json
espbench release mi-board --json
```

| Exit | `error` |
|---|---|
| 0 | ok |
| 1 | `bad_request` / `unexpected` |
| 2 | `flash_failed` |
| 3 | `crashed` (panic, boot loop o reinicio en la ventana) |
| 4 | `timeout` |
| 5 | `busy` |
| 6 | `locked` / `reservation_lost` / `token_mismatch` |
| 7 | `not_found` / `device_changed` / `session_down` |
| 8 | `bad_anchor` / `cursor_expired` |
| 9 | `session_ended` |
| 10 | `network` / `auth` / `auth_config` |

**Config** (gana la primera): flags (`--host`, `--profile`) > env (`ESPBENCH_HOST`, `ESPBENCH_TOKEN`, `ESPBENCH_USER`,
`ESPBENCH_LOCK_TOKEN`) > `~/.config/espbench.json` > `remote` del `.flashcfg.json` del proyecto (host, token,
lock_user, lock_token; chip, `flash_baud` y `encrypt` también salen de ahí).

```json
{"default_profile": "lab",
 "profiles": {"lab": {"host": "sensipi03", "token": "", "lock_user": "alejo-agent", "lock_token": "..."}}}
```

`host` acepta `host`, `host:puerto` (default 8080, el dashboard) o una URL. El flash va por TCP al puerto de la
placa, que el CLI saca de `/api/devices`.

**Skill para Claude Code** (`client/agent/SKILL.md`: cuándo usar `espbench`, el ciclo, qué hacer con cada `error`,
cómo no llenar el contexto de log):

```bash
mkdir -p ~/.claude/skills && ln -sfn "$PWD/client/agent" ~/.claude/skills/espbench    # desde la raíz del repo
```

Probar sin Pi: `ESP_BASE=$(mktemp -d) python -m tests.benchsim --port 8099` levanta una Pi simulada (API real, una
placa que bootea, contesta `status` y crashea con `panic`); después `ESPBENCH_HOST=127.0.0.1:8099 espbench ls`.

---

## Dashboard

Web UI at `http://<pi-ip>:8080`. Shows all connected devices, firmware info, and real-time serial logs via WebSocket.

### API token (optional)

Create `/opt/esp/api_token` (one line, the token) to require `Authorization: Bearer <token>` on every write of the
dashboard API (send, reset, reserve, unlock, rename...). Reads stay open. The dashboard asks for the token once and keeps
it in the browser.

> ⚠️ The same file is the **flash token** (unless the session runs with `--token`): as soon as it exists, every
> `.flashcfg.json` without `remote.token` (or with a different one) **stops being able to flash** (`unauthorized`).
> Set `remote.token` in each project before creating the file. Delete the file (or leave it empty) to go back to no auth.
>
> Reads stay open on purpose: the serial log, the events and the text of every console `send` are visible to anyone
> on the network. Don't type secrets in the serial console.

---

## Configuration Reference (`.flashcfg.json`)

| Field | Description |
|-------|-------------|
| `mode` | `local` \| `remote` \| `auto` \| `custom` |
| `paths.project_root` | ESP-IDF project root |
| `paths.idf_py` | Path to `idf.py` (optional, auto-detected) |
| `local.port` | Serial port for local flash |
| `local.monitor` | Open monitor after flash |
| `remote.host` | Pi IP or hostname |
| `remote.port` | TCP port (`5000 + device index`) |
| `remote.token` | Server auth token (= `/opt/esp/api_token` on the Pi, if it exists) |
| `remote.lock_user` | Username for device locking |
| `remote.lock_token` | Token for device locking |
| `chip` | ESP chip model (`esp32`, `esp32s3`, etc.) |
| `flash_baud` | Flash baud rate |
| `encrypt` | Flash encryption enabled |
| `erase` | Erase flash before writing |

---

## Project Structure

```
espbench/
├── common.py                  # Shared: TCP framing, SHA256, MAC↔SN, HW model utils
├── client/
│   ├── deploy.py              # CLI for humans: build + artifact + remote/local flash
│   ├── espbench.py            # CLI for agents (`espbench`, --json, exit codes)
│   ├── espbench_lib.py        # Client library: config, HTTP API, log ranges with waits, flash + verify
│   └── agent/SKILL.md         # Claude Code skill for `espbench`
├── remote/
│   ├── server/
│   │   ├── remote_esp32.py    # Per-device process: wiring, MAC discovery, threads
│   │   ├── device.py          # TtyPort / Device (FSM) / DeviceManager
│   │   ├── device_log.py      # DeviceLog: the device's log (single writer)
│   │   ├── monitor.py         # EspMonitor: esp_idf_monitor in a PTY
│   │   ├── protocol.py        # TCP flash protocol, split into testable phases
│   │   ├── erase.py           # Interactive Erase Region (Ctrl-E)
│   │   ├── flash.py           # esptool: find, build command, run, read MAC
│   │   ├── api.py             # Dashboard backend: REST + WebSocket
│   │   ├── device_registry.py # Dashboard's read model of the devices
│   │   ├── log_streamer.py    # Log tail → WebSocket
│   │   ├── runstate.py        # run/<tty>.json (runtime state between processes)
│   │   ├── paths.py           # Every path under ESP_BASE
│   │   └── taglog.py          # taglog.info(TAG, msg) logging
│   ├── dashboard/             # Web UI: index.html, device.html, style.css
│   └── infra/                 # devremote, esp32_tmux.sh, espbench-name, udev, systemd
├── tests/                     # pytest suite (host, no hardware)
├── docs/                      # ARCHITECTURE.md; archive/ = old PRDs and code
├── rpi/                       # Pi bootstrap script
└── scripts/                   # Maintenance scripts
```

---

## Flash Workflow (Remote)

1. `deploy.py` reads `.flashcfg.json`
2. Optionally runs `idf.py build`
3. Zips `flasher_args.json` + `*.bin` + `firmware.elf` → `artifact.zip`
4. TCP connect to Pi → send header (token, chip, job metadata, lock user/token)
5. Pi checks token, device state (rejects with `device_busy` during an erase) and lock, then ACKs
6. Upload artifact, Pi verifies SHA256
7. Pi: device → FLASHING, stops monitor → `esptool write_flash` (retries without `--encrypt` on rc=2) → restarts monitor → back to MONITORING
8. Client receives result JSON (esptool output is streamed live while flashing), sent once the monitor is back up; it carries the `cursor` of the `flash` event in the device log

---

## Server Paths (Raspberry Pi)

```
/opt/esp/
├── server/, dashboard/, venv/    code + Python env
├── devices.json                  MAC → device_key + hw_model
├── slots.conf                    (optional) stable ports: <K> <ID_PATH>
├── run/<tty>.json                runtime state of each session (state, MAC, port, log path)
├── devices/<MAC>/
│   ├── output.log                current session log (serial + server events)
│   ├── output_<ts>.log           previous sessions
│   ├── current.elf               last flashed ELF (backtrace decoding)
│   ├── last_user
│   └── jobs/<job_id>/            extracted artifact + job.log
├── locks/<tty>                   device lock (user:token[:expires[:mac]])
├── api_token                     (optional) API + flash token
└── VERSION
```

Devices whose MAC can't be read keep the per-tty layout (`devices/unknown-<tty>/`, `jobs/`, `current_<tty>.elf`).

---

## Tests

```bash
pytest tests/
```

No hardware required. The protocol, the per-device entrypoint and the infra shell scripts run end-to-end against fakes at the edges (esptool, esp_idf_monitor, socket, tmux, udev). What can only be verified on a real Pi is listed in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §10.

---

## Device Management (Pi)

```bash
devremote                 # start missing sessions
devremote --status        # device / kernel tty / port / session / FSM state / pid
devremote 0               # attach to a device's session (0 = ttyUSB0; also esp-slotK, slotK)
devremote --reset         # restart all sessions
devremote --reset 0       # restart one device
devremote --unlock 0      # release a device lock
devremote --slots         # ID_PATH of each physical port (for slots.conf)
devremote --cleanup       # delete old jobs and rotated logs (--dry-run to preview)
```

### Stable ports (optional)

`ttyUSBN` follows the kernel's enumeration order, not the physical port: a replug or reboot can swap them, and with them the TCP port your `.flashcfg.json` points to. To pin ports to physical hub slots:

```bash
devremote --slots                                  # shows each port's ID_PATH
sudo nano /opt/esp/slots.conf                      # one line per port: "<K> <ID_PATH>"
sudo udevadm trigger --subsystem-match=tty && devremote --reset
```

Mapped devices become `esp-slotK` on port `5000+K`. Without `slots.conf` nothing changes. With it, unmapped devices get `5100+N` so they don't collide with slot ports.


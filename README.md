# espbench

Remote ESP32 firmware deployment system. Build on your dev machine, flash to an ESP32 connected to a Raspberry Pi over TCP. Includes a persistent serial monitor and web dashboard.

**Version:** 0.4.4

---

## Architecture

```
Dev machine                          Raspberry Pi
────────────                         ────────────────────────────────────────
client/deploy.py  ──── TCP ────►  remote/server/remote_esp32.py
  │                                        │
  │  1. idf.py build (optional)            ├─ EspMonitor (PTY, always-on)
  │  2. zip firmware artifact              │   └─ esp_idf_monitor + ELF backtrace
  │  3. send over TCP                      │
  │  4. receive result                     ├─ control_server (TCP, port 5000+N)
  │                                        │   └─ stop monitor → esptool → restart
  └─ .flashcfg.json                        │
     (local gitignored config)             └─ dashboard.py (FastAPI, port 8080)
                                               └─ /api/devices, /ws/device/{tty}
```

One tmux session per device (`esp32_ttyUSBN`). TCP port = `5000 + N`. udev auto-creates sessions on USB plug-in.

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
sudo git pull && sudo bash remote/install.sh && sudo systemctl restart dashboard devremote
```

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

## Dashboard

Web UI at `http://<pi-ip>:8080`. Shows all connected devices, firmware info, and real-time serial logs via WebSocket.

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
| `remote.token` | Server auth token |
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
├── common.py              # Shared: TCP framing, SHA256, MAC↔SN, HW model utils
├── client/
│   └── deploy.py          # CLI: build + artifact + remote/local flash
├── remote/
│   ├── server/
│   │   ├── remote_esp32.py    # Entrypoint: args, logging, monitor + TCP startup
│   │   ├── protocol.py        # TCP control: flash orchestration, device locking
│   │   ├── flash.py           # esptool: find, build command, execute
│   │   ├── monitor.py         # Serial monitor: PTY, circular buffer, log rotation
│   │   ├── device_registry.py # Device metadata: MAC, SN, fw info, status
│   │   └── dashboard.py       # FastAPI: REST API + WebSocket log streaming
│   ├── dashboard/             # Web UI: index.html, device.html, style.css
│   └── infra/                 # systemd services, udev rules, devremote CLI
├── tests/                     # pytest suite
├── rpi/                       # Pi bootstrap script
└── scripts/                   # Maintenance scripts
```

---

## Flash Workflow (Remote)

1. `deploy.py` reads `.flashcfg.json`
2. Optionally runs `idf.py build`
3. Zips `flasher_args.json` + `*.bin` + `firmware.elf` → `artifact.zip`
4. TCP connect to Pi → send header (token, chip, job metadata)
5. Upload artifact, Pi verifies SHA256
6. Pi: stops monitor → runs `esptool write_flash` → restarts monitor
7. Client receives result JSON

---

## Server Paths (Raspberry Pi)

```
/opt/esp/
├── server/            deploy of remote/server/
├── logs/YYYYMMDD/     daily serial logs per device
├── jobs/              extracted artifacts per flash job
├── locks/             device lock files
├── devices.json       MAC → device_key + hw_model registry
├── current.elf        last flashed ELF (backtrace decoding)
└── VERSION            version file
```

---

## Tests

```bash
pytest tests/
```

No hardware required — device registry and monitor are fully mocked.

---

## Device Management (Pi)

```bash
devremote           # scan + start missing sessions
devremote --status  # table: device / port / status / PID
devremote 0         # attach to ttyUSB0 session
devremote --reset   # kill + restart all sessions
devremote --unlock 0  # unlock ttyUSB0
```

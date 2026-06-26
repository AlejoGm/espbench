# espbench — Root

Remote ESP32 firmware flash + serial monitor system. Runs Python client on dev machine, Python server on Raspberry Pi.

## Project Structure

```
espbench/
├── common.py          # Shared: framing, SHA256, MAC↔SN conversion
├── client/            # Developer machine: build + flash client
├── remote/            # Raspberry Pi: flash server + web dashboard
│   ├── server/        # Core server logic (TCP, monitor, device registry)
│   ├── dashboard/     # Web UI (HTML/CSS/JS, served by FastAPI)
│   └── infra/         # systemd services, udev rules, devremote CLI
├── tests/             # pytest test suite
├── rpi/               # Full Pi setup script
└── issues/            # Issue tracking docs
```

## Key Architecture

- Client (`deploy.py`) connects TCP to server on Pi, sends zipped firmware artifact
- Server stops serial monitor, runs esptool, restarts monitor
- Dashboard (FastAPI + vanilla JS) streams serial logs via WebSocket
- Devices managed via tmux sessions, one per `/dev/ttyUSBN`
- TCP ports: `5000 + N` per device (N = ttyUSBN index)

## Shared Module

`common.py` imported by both client and server:
- `send_msg(sock, obj)` / `recv_msg(sock)` — 4-byte length-prefixed JSON framing
- `sha256_file(p)` — chunked SHA256
- `mac_to_sn_sfy(mac)` / `sn_sfy_to_mac(sn)` — MAC↔Sensify SN
- `hw_model_from_project_name(name)` — extract HW model from project name

## Server Install (Pi)

```bash
sudo ./remote/install.sh
# Deploys to /opt/esp/
```

## Paths on Pi

```
/opt/esp/server/     → remote/server/ copy
/opt/esp/logs/       → daily serial logs + job logs
/opt/esp/jobs/       → extracted artifacts
/opt/esp/locks/      → device lock files
/opt/esp/current.elf → last flashed ELF (backtrace decoding)
```

## Versioning

`VERSION` at repo root. Current: `0.4.4`.

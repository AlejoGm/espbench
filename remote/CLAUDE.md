# remote/

All code that runs on Raspberry Pi.

## Subdirectories

| Dir | Purpose |
|-----|---------|
| `server/` | Core flash server: TCP control, serial monitor, device registry, FastAPI dashboard |
| `dashboard/` | Web UI static files (HTML/CSS/JS) served by `server/dashboard.py` |
| `infra/` | systemd services, udev rules, `devremote` CLI |

## Install

```bash
sudo ./install.sh
```

Idempotent. Creates `/opt/esp/` dirs, copies server files, installs udev rules and systemd services.

## Dependencies (`requirements.txt`)

```
esptool
esp-idf-monitor
fastapi
uvicorn[standard]
```

## Device Management Model

- One tmux session per device: `esp32_ttyUSBN`
- Each session runs `remote_esp32.py` bound to `/dev/ttyUSBN`
- TCP port = `5000 + N`
- udev rule auto-creates sessions on USB plug-in (`99-esp32.rules`)
- `devremote` CLI for manual session management

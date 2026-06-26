# rpi/

Raspberry Pi initial setup utilities.

## Files

| File | Purpose |
|------|---------|
| `pi-setup.sh` | Full Pi bootstrap: system deps, Python venv, directory creation, service install |

## pi-setup.sh

Idempotent setup script for fresh Pi. Runs as root.

Steps:
- Install system packages (Python3, pip, tmux, udev tools)
- Create Python venv at `/opt/esp/venv`
- Install Python deps from `remote/requirements.txt`
- Create `/opt/esp/` directory structure
- Call `remote/install.sh` to deploy server files + services

Run once on a new Pi:
```bash
sudo bash rpi/pi-setup.sh
```

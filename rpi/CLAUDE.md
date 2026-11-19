# rpi/

Raspberry Pi initial setup utilities.

## Files

| File | Purpose |
|------|---------|
| `pi-setup.sh` | Full Pi bootstrap: system deps, Python venv, directory creation, service install |
| `wifi-provision.sh` | Loop de `wifi-provision.service`: AP `SfyBench` (wifi-connect) solo si no hay WiFi guardada o wlan0 lleva 10 min sin red. Nunca por un corte de internet: la Pi 4 tiene una sola radio y con el AP prendido el bench queda offline. Tests: `tests/test_wifi_provision.py` |
| `portal/` | UI del portal de wifi-connect |

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

# remote/

Todo lo que corre en la Raspberry Pi. Arquitectura: [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md).

| Dir | |
|---|---|
| `server/` | Python: un proceso por device (`remote_esp32.py`) + backend del dashboard (`api.py`) |
| `dashboard/` | Frontend del dashboard (HTML/CSS/JS, sin build), servido por `server/api.py` |
| `infra/` | `devremote`, `esp32_tmux.sh`, `espbench-name`, udev, systemd, `update.sh` |

## Instalar / actualizar

```bash
sudo bash install.sh            # idempotente: /opt/esp/, venv, udev, systemd
sudo bash infra/update.sh       # desde el clone: pull + install + restart
```

## Modelo de procesos

- Una sesión tmux `esp32_<nombre>` por device, corriendo `remote_esp32.py`.
- `<nombre>` y puerto TCP salen de `infra/espbench-name`: `ttyUSBN` → `5000+N`, o `esp-slotK` → `5000+K` si el puerto físico está mapeado en `/opt/esp/slots.conf`.
- Hotplug: udev → systemd (`espbench-attach@`) → `devremote --start`. Al boot: `devremote.service`.
- El dashboard (`dashboard.service`) es un proceso aparte que lee el estado de cada device de `/opt/esp/run/<tty>.json`.

## Dependencias (`requirements.txt`)

```
esptool
esp-idf-monitor
fastapi
uvicorn[standard]
```

# remote/infra/

Infraestructura de la Pi: sesiones tmux por device, nombres y puertos, udev, systemd. Arquitectura: [../../docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §6-7.

## Archivos

| Archivo | Tipo | Qué hace | Instalado en |
|---|---|---|---|
| `espbench-name` | bash | **Única fuente** de la regla de nombre y puerto de un device | `/usr/local/bin/` |
| `esp32_tmux.sh` | bash | Crea la sesión tmux `esp32_<nombre>` que corre `remote_esp32.py` | `/usr/local/bin/` |
| `devremote` | bash | CLI de sesiones (ver abajo). Siempre corre como `sfypi` | `/usr/local/bin/` |
| `99-esp32.rules` | udev | Symlink `/dev/esp-slotK` + hotplug vía systemd | `/etc/udev/rules.d/` |
| `espbench-attach@.service` | systemd | Hotplug: `devremote --start %I` como `sfypi` | `/etc/systemd/system/` |
| `devremote.service` | systemd | Al boot: levanta las sesiones de lo que ya esté enchufado | `/etc/systemd/system/` |
| `dashboard.service` | systemd | `uvicorn server.api:app` en el puerto 8080 | `/etc/systemd/system/` |
| `update.sh` | bash | Actualizar una Pi: fetch/pull + `install.sh` + restart dashboard + `devremote --reset` | (se corre desde el clone) |

## Nombres y puertos (`espbench-name`)

| Caso | Nombre | Puerto |
|---|---|---|
| Sin `/opt/esp/slots.conf` | `ttyUSBN` | `5000+N` |
| Puerto físico mapeado | `esp-slotK` | `5000+K` |
| Hay `slots.conf` y el device no está mapeado | `ttyUSBN` | `5100+N` |

`slots.conf`: una línea `<K> <ID_PATH>` por puerto físico del hub. Para armarlo, `devremote --slots`, y después `sudo udevadm trigger --subsystem-match=tty && devremote --reset`.

**No duplicar esta regla** en otro script ni en Python. Python recibe el puerto por `--control-port`.

## devremote

| Uso | |
|---|---|
| `devremote` | Levanta las sesiones que falten |
| `devremote <dev>` | `tmux attach` a la sesión del device |
| `devremote --status` | Device / kernel tty / puerto / sesión / estado de la FSM / pid |
| `devremote --reset [<dev>]` | Reinicia todas las sesiones, o solo la de `<dev>` |
| `devremote --unlock <dev>` | Libera el lock |
| `devremote --slots` | `ID_PATH` de cada puerto (para armar `slots.conf`) |
| `devremote --start <ttyUSBN>` | Levanta una sesión (lo usa el hotplug) |
| `devremote --cleanup [--dry-run] [--jobs-days N] [--logs-days N]` | Borra jobs y sesiones de log viejas (nunca la sesión actual) |

`<dev>` acepta `N` (= `ttyUSBN`), `ttyUSBN`, `esp-slotK` o `slotK`.

## Hotplug

udev → `espbench-attach@<tty>.service` → `devremote --start`. **No** usar `RUN+=` desde udev para lanzar tmux: corre en el tmux server de root y lo mata el fin del evento. La regla anterior hacía exactamente eso, y por eso el hotplug nunca funcionó.

## Tests

`tests/test_infra.py` corre estos scripts reales con `tmux`/`udevadm`/`pkill`/`sudo` falsos en el `PATH`. Para eso existen `ESPBENCH_DEV_DIR` y `DEVREMOTE_NO_REEXEC`, que **son solo para tests**. La regla udev y los units de systemd solo se verifican en la Pi.

# remote/infra/

Infraestructura de la Pi: sesiones tmux por device, nombres y puertos, udev, systemd. Arquitectura: [../../docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §6-7.

## Archivos

| Archivo | Tipo | Qué hace | Instalado en |
|---|---|---|---|
| `espbench-name` | bash | **Única fuente** de la regla de nombre y puerto de un device | `/usr/local/bin/` |
| `esp32_tmux.sh` | bash | Crea la sesión tmux `esp32_<nombre>` que corre `remote_esp32.py`. Una sesión existente se deja solo si su proceso está vivo; sin proceso, `disconnected` o `stopping` (esperando a que salga), se recrea | `/usr/local/bin/` |
| `espbench-procs` | bash | **Única fuente** de "cuáles son los procesos de una placa": `sudo`/`python*` con `remote_esp32.py -p <tty>` en argv[0] (no el tmux server, cuyo cmdline lleva el comando entero) + descendientes (esptool, esp_idf_monitor). `--roots`: sin descendientes | `/usr/local/bin/` |
| `devremote` | bash | CLI de sesiones (ver abajo). Siempre corre como `sfypi` | `/usr/local/bin/` |
| `99-esp32.rules` | udev | Symlink `/dev/esp-slotK` + hotplug vía systemd | `/etc/udev/rules.d/` |
| `espbench-attach@.service` | systemd | Hotplug: `devremote --start %I` como `sfypi` | `/etc/systemd/system/` |
| `devremote.service` | systemd | Al boot: levanta las sesiones de lo que ya esté enchufado. Espera a `time-sync.target` (la Pi no tiene RTC); `install.sh` habilita `systemd-time-wait-sync` con tope de 90 s | `/etc/systemd/system/` |
| `dashboard.service` | systemd | `uvicorn server.api:app` en el puerto 8080 | `/etc/systemd/system/` |
| `espbench-update` | bash | Update del bench con rollback: release (tag `vX.Y.Z`), o la ref fijada (PIN). Después del reset verifica una sesión viva por placa (`devremote --check`), reintenta el reset una vez y si sigue faltando deja `warning` en el status. `--setup <repo>` escribe `update.conf` (lo llama `install.sh`). Ver ARCHITECTURE §13 | `/usr/local/bin/` |
| `espbench-update.service` / `.timer` | systemd | `espbench-update --auto` 3 min después del boot y a las 04:00 (± 20 min) | `/etc/systemd/system/` |
| `update.sh` | bash | Atajo de `espbench-update` desde el clone: sin args, la rama del clone; `update.sh <ref>`; `--release` | (se corre desde el clone) |
| `pip-deps.sh` | bash | Dependencias Python del venv (lo llama `install.sh`): `regex` aparte y opcional, su falla avisa y no aborta el install | (se corre desde el clone) |

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
| `devremote --status` | Device / kernel tty / puerto / sesión (`RUNNING`, `STALE` = sesión sin proceso, `DOWN`) / estado de la FSM / pid |
| `devremote --reset [<dev>]` | Reinicia todas las sesiones, o solo la de `<dev>`: `kill -9` a los procesos de la placa y sus hijos (`espbench-procs`), espera a que mueran (`DEVREMOTE_KILL_TIMEOUT`, 5 s; si no, `-9` de nuevo) y recién ahí recrea la sesión |
| `devremote --check` | Sale con 0 si cada device enchufado tiene sesión con su `remote_esp32` vivo; lista lo que falta (lo usa `espbench-update`) |
| `devremote --unlock <dev>` | Libera el lock o la reserva (de quien sea), bajo el `flock` de `locks.exclusive` y con un evento `release` forzado (`python3 -m server.locks unlock`; sin el server instalado, `rm`) |
| `devremote --slots` | `ID_PATH` de cada puerto (para armar `slots.conf`) |
| `devremote --start <ttyUSBN>` | Levanta una sesión (lo usa el hotplug) |
| `devremote --cleanup [--dry-run] [--jobs-days N] [--logs-days N]` | Borra jobs y sesiones de log viejas (nunca la sesión actual) |

`<dev>` acepta `N` (= `ttyUSBN`), `ttyUSBN`, `esp-slotK` o `slotK`.

## Hotplug

udev → `espbench-attach@<tty>.service` → `devremote --start`. **No** usar `RUN+=` desde udev para lanzar tmux: corre en el tmux server de root y lo mata el fin del evento. La regla anterior hacía exactamente eso, y por eso el hotplug nunca funcionó.

## El tmux server y los cgroups de systemd

El tmux server nace en el cgroup de quien crea la primera sesión. Si es un unit que mata su cgroup al terminar (el `systemd-run` del update, `dashboard.service` en un restart), mueren todas las placas. Por eso `esp32_tmux.sh`, cuando no hay server, crea la sesión con `sudo systemd-run --scope --uid=sfypi`. Cualquier unit nuevo que lance `devremote` y no sea `RemainAfterExit` va con `KillMode=process` (como `espbench-attach@` y `espbench-update.service`).

## Tests

`tests/test_update.py` corre `espbench-update` con git de verdad (origin bare con releases y ramas) y `install.sh`/`systemctl`/`devremote`/`curl` falsos: releases por versión, PIN, ocupado, rollback, lock, verificación de sesiones (`devremote --check`) con reintento y `warning`.

`tests/test_infra.py` corre estos scripts reales con `tmux`/`udevadm`/`pkill`/`sudo`/`systemd-run` falsos en el `PATH`. Para eso existen `ESPBENCH_DEV_DIR` y `DEVREMOTE_NO_REEXEC`, que **son solo para tests**. La regla udev y los units de systemd solo se verifican en la Pi.

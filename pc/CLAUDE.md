# pc/

Setup de una PC x86 con Ubuntu (Server o Desktop) como bench: lo que en una Pi hace `rpi/pi-setup.sh`.
Pasos completos (BIOS, instalador, clone, install): `README.md` § Fresh Ubuntu PC Setup.

| Archivo | Qué hace |
|---|---|
| `pc-setup.sh` | `apt full-upgrade`, paquetes de `install.sh` + `openssh-server` y `avahi-daemon`, saca `brltty` y `modemmanager` (en Ubuntu Desktop toman los puertos serie), enmascara `sleep`/`suspend`/`hibernate`, hostname (`BENCH_HOSTNAME`) y zona horaria (`BENCH_TZ`) opcionales, Tailscale con `tag:bench` si hay `TS_AUTHKEY`. No corre `install.sh` ni toca el escritorio gráfico |

Después: `sudo bash remote/install.sh` y reiniciar. Lo que cambia con respecto a una Pi del lado del server está en
`remote/server/benchinfo.py` (temperatura de la CPU y modelo del DMI); el resto (udev, systemd, tmux) es igual.

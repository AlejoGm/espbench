# #07 — `install.sh` — deploy reproducible a la Pi

## What to build

Script bash idempotente que despliega el sistema completo desde el repo a una Raspberry Pi (o cualquier Linux). Requiere correr como root.

Acciones en orden:
1. Copia `server/` → `/opt/esp/` (crea el dir si no existe)
2. Copia `infra/devremote` → `/usr/local/bin/devremote` (chmod +x)
3. Copia `infra/esp32_tmux.sh` → `/usr/local/bin/esp32_tmux.sh` (chmod +x)
4. Copia `infra/99-esp32.rules` → `/etc/udev/rules.d/99-esp32.rules`
5. Copia `infra/devremote.service` → `/etc/systemd/system/devremote.service`
6. `udevadm control --reload-rules && udevadm trigger`
7. `systemctl daemon-reload && systemctl enable devremote`
8. Imprime resumen de qué se instaló y dónde

Idempotente: correrlo dos veces no rompe nada (sobreescribe silenciosamente).

El script verifica al inicio que está corriendo como root y que el sistema tiene `tmux`, `python3`, `pip3` disponibles. Si falta algo, imprime instrucciones y sale con código ≠ 0.

## Acceptance criteria

- [ ] `./install.sh` corre sin errores en una Pi limpia (como root)
- [ ] Después del install, `devremote --status` funciona
- [ ] Después del install, `systemctl status devremote` muestra enabled
- [ ] Correr `./install.sh` dos veces no produce errores
- [ ] Si no corre como root, sale con mensaje claro

## Blocked by

- #01

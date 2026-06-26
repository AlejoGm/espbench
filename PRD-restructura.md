# PRD — Reestructura remoteFlashServer

> Estado: ready-for-implementation  
> Fecha: 2026-06-21  
> Contexto: conversación de diseño + análisis de codebase

---

## Problem Statement

El sistema de flasheo y monitoreo remoto de ESP32 creció orgánicamente a un archivo monolítico de ~1200 líneas que mezcla tres responsabilidades distintas (protocolo TCP, monitor serial, lógica de flash). Los archivos de infraestructura que hacen funcionar el sistema en la Raspberry Pi (orquestador de sesiones tmux, reglas udev, units de systemd) viven dispersos en el filesystem de la Pi sin estar versionados. Si la Pi muere o hay que replicar el setup, el sistema solo existe en la memoria del desarrollador.

Adicionalmente, el monitor serial actual (picocom) no decodifica backtraces de ESP32, lo que obliga a tener una sesión separada con IDF para diagnosticar panics.

---

## Solution

Reestructurar el proyecto en módulos separados por responsabilidad, traer toda la infraestructura de la Pi al repositorio con un script de instalación reproducible, y reemplazar picocom por `esp-idf-monitor` para decodificación de backtraces en tiempo real. El resultado es un sistema que se puede reproducir desde cero con `git clone + ./install.sh`.

---

## User Stories

1. Como desarrollador, quiero que el repositorio contenga todos los archivos del sistema (código Python + infraestructura de la Pi), para poder reconstruir el setup completo desde cero sin depender de memoria.

2. Como desarrollador, quiero ejecutar `./install.sh` en la Pi para desplegar udev rules, systemd units y scripts de orquestación a sus ubicaciones correctas.

3. Como desarrollador, quiero que el servidor de flash esté separado en módulos (`monitor`, `flash`, `protocol`), para poder modificar el monitor sin tocar la lógica de flash y viceversa.

4. Como desarrollador, quiero que el código de framing TCP y SHA256 esté en un único lugar compartido (`common.py`), para que cliente y servidor no puedan desincronizarse silenciosamente.

5. Como desarrollador, quiero que el monitor serial decodifique backtraces de ESP32 en tiempo real, para poder diagnosticar panics sin abrir una sesión separada con IDF.

6. Como desarrollador, quiero que el archivo `.elf` del build se envíe automáticamente al servidor en modo remoto, para que el monitor tenga los símbolos necesarios para decodificar backtraces.

7. Como desarrollador, quiero que el servidor guarde el último `.elf` recibido y lo use al reiniciar el monitor después de cada flash, para que la decodificación refleje siempre el firmware más reciente.

8. Como desarrollador, quiero que si no hay `.elf` disponible (primer arranque antes de flashear), el monitor funcione igual que antes (sin decodificación), para no romper el flujo existente.

9. Como desarrollador, quiero que `devremote --status` siga funcionando para ver qué sesiones tmux están corriendo y en qué puertos, para mantener la visibilidad que tengo hoy.

10. Como desarrollador, quiero que `devremote --reset` reinicie todas las sesiones, para poder recuperar el sistema sin reiniciar la Pi.

11. Como desarrollador, quiero que conectar un ESP32 por USB dispare automáticamente una sesión de monitor+flash via udev, para no tener que ejecutar nada manualmente al enchufar un dispositivo.

12. Como desarrollador, quiero que el servicio de orquestación arranque automáticamente al bootear la Pi, para que el sistema esté listo sin intervención manual.

13. Como desarrollador, quiero un `requirements.txt` que declare todas las dependencias Python del servidor, para poder reproducir el entorno de la Pi.

14. Como desarrollador, quiero que el archivo `ARCHITECTURE.md` refleje la estructura final del sistema, para tener documentación de referencia actualizada.

15. Como desarrollador, quiero que el cliente detecte automáticamente el `.elf` en `build/*.elf` y lo incluya en el artifact ZIP solo en modo remoto, para que el flujo de deploy no requiera pasos manuales adicionales.

---

## Implementation Decisions

### Estructura de carpetas

```
remoteFlashServer/
├── server/
│   ├── remote_esp32.py    ← entrypoint: argparse, setup, loop principal
│   ├── monitor.py         ← EspMonitor (reemplaza PicocomMonitor)
│   ├── flash.py           ← build_esptool_cmd, run_cmd, find_esptool
│   └── protocol.py        ← handle_control, control_server, recv_exact
├── client/
│   └── deploy.py          ← cliente de flash (sin cambios funcionales)
├── common.py              ← sha256_file, framing TCP (send_msg/recv_msg)
├── infra/
│   ├── devremote          ← orquestador tmux (copiado de Pi)
│   ├── esp32_tmux.sh      ← wrapper udev → tmux
│   ├── 99-esp32.rules     ← udev rule
│   └── devremote.service  ← systemd oneshot
├── install.sh             ← despliega infra/ a la Pi
├── requirements.txt
└── ARCHITECTURE.md
```

### Módulo `common.py`

Contiene exactamente dos cosas:
- `sha256_file(path) -> str` — hoy duplicado como `sha256_file` en el cliente y en el servidor con misma implementación
- Framing TCP: `send_msg(sock, obj)` y `recv_msg(sock) -> dict` — hoy duplicado como `recvall`/`struct.pack` inline en ambos lados, con nombres distintos

No crece más allá de esto. No es un dumping ground.

### Módulo `server/monitor.py` — EspMonitor

Reemplaza `PicocomMonitor`. Misma interfaz: `start()`, `stop()`, `restart()`, `get_recent_output()`.

Diferencias:
- Usa `esp-idf-monitor` en lugar de `picocom` (`pip install esp-idf-monitor`)
- Acepta parámetro opcional `elf_path: Path | None`
- Si `elf_path` es None o no existe, corre sin decodificación (fallback silencioso)
- Ctrl-E (erase_region_interactive) se mantiene sin cambios

El servidor guarda el `.elf` recibido en `{base}/current.elf`. Al hacer `monitor.restart()` post-flash, apunta al `current.elf` actualizado.

### Módulo `server/flash.py`

Extraído de `remote_esp32.py` sin cambios funcionales:
- `find_esptool_cmd() -> list[str]`
- `build_esptool_cmd(...) -> tuple[cmd, cmd, pairs]`
- `run_cmd(cmd, log) -> int`

Se elimina el hardcodeo de `/home/sfypi/espvenv/bin/esptool.py`. Solo PATH + `python -m esptool` como fallback.

### Módulo `server/protocol.py`

Extraído de `remote_esp32.py`:
- `handle_control(sock, cfg, mon, svc_log)` — lógica completa de una sesión de flash
- `control_server(cfg, mon, svc_log)` — loop TCP accept

Usa `common.recv_msg` / `common.send_msg` en lugar del framing inline actual.

### `.elf` en artifact (cliente)

En `client/deploy.py`, función `collect_artifact()`:
- En modo remote: busca `build/*.elf` (primer match). Si existe, lo agrega al ZIP.
- En modo local: no lo agrega.
- Si no hay `.elf` en el build dir: warning, continúa sin él.

En el servidor, `handle_control()` extrae el `.elf` del ZIP si está presente y lo copia a `{base}/current.elf` antes de reiniciar el monitor.

### `install.sh`

Script bash que:
1. Copia `infra/devremote` → `/usr/local/bin/devremote` (chmod +x)
2. Copia `infra/esp32_tmux.sh` → `/usr/local/bin/esp32_tmux.sh` (chmod +x)
3. Copia `infra/99-esp32.rules` → `/etc/udev/rules.d/99-esp32.rules`
4. Copia `infra/devremote.service` → `/etc/systemd/system/devremote.service`
5. Copia `server/` → `/opt/esp/`
6. Ejecuta `udevadm control --reload-rules && systemctl daemon-reload && systemctl enable devremote`

Requiere correr como root. Idempotente (sobreescribe).

### `requirements.txt`

```
esptool
esp-idf-monitor
```

### Protocolo TCP — sin cambios funcionales

El protocolo existente se mantiene igual. `common.py` lo encapsula pero no lo modifica. No se agrega versioning en esta iteración (out of scope).

---

## Testing Decisions

Un buen test verifica comportamiento observable desde afuera del módulo, no detalles de implementación interna. No mockear lo que se puede ejercitar directamente.

### Módulos a testear

**`common.py`** — prioridad alta, bajo costo:
- `sha256_file`: crear archivo temp con contenido conocido, verificar digest
- `send_msg` / `recv_msg`: usar `socket.socketpair()`, enviar objeto, verificar que se recibe igual

**`server/flash.py`** — prioridad media:
- `build_esptool_cmd`: crear jobdir con `flasher_args.json` de ejemplo, verificar que los pares offset/archivo son correctos. Testear los tres casos: `flash_files` como dict, como list, y fallback por nombre.

**`client/deploy.py` — `collect_artifact()`** — prioridad media:
- Crear un `build/` falso con archivos `.bin` y `.elf`, verificar que el ZIP resultante contiene los archivos correctos en modo remote vs local.

### Fuera del scope de tests

- `monitor.py` — depende de PTY real y hardware serial. No testeable en host.
- `protocol.py` — integración completa. Requiere mock de socket + monitor. Complejidad alta, valor bajo en esta etapa.
- `infra/` — scripts de sistema. Verificar manualmente post-install.

---

## Out of Scope

- Dashboard web / HTTP API / WebSocket streaming (norte futuro, no bloquea esta iteración)
- Versioning del protocolo TCP
- Tests de integración end-to-end (requieren hardware)
- CI/CD pipeline
- Multi-device en un solo proceso (el orquestador tmux cubre esto)
- `flash_existing` action (era de `flashd`, descartado)
- TLS en el servidor (era de `flashd`, descartado)

---

## Further Notes

- `flashd(server).py` fue descartado. Era una versión alternativa que nunca llegó a producción. El sistema productivo usa solo `remote_esp32(server).py`.
- `devremote(server).sh` en el repo era una copia incorrecta de `deploy(client).py`. Se reemplaza por el `devremote` real copiado de la Pi.
- El orquestador (`devremote` + udev + systemd) es hoy el "monitor de monitores primitivo". La arquitectura modular de esta reestructura es el paso previo necesario para evolucionar hacia un dashboard web en el futuro.
- Antes de implementar, traer de la Pi: `devremote`, `esp32_tmux.sh`, `99-esp32.rules`, `devremote.service`.

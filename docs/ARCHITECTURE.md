# remoteFlashServer — Arquitectura

> Estado: 2026-06-23. Documento descriptivo, no normativo.

---

## Visión general

Sistema Python para flashear firmware ESP32 de forma local o remota. El desarrollador corre `deploy.py` desde su máquina; en la Raspberry Pi corre `remote_esp32.py`, que mantiene un monitor serial persistente y acepta conexiones de flash por TCP.

```
[Máquina del desarrollador]           [Raspberry Pi / servidor remoto]
        client/deploy.py  ──TCP──►  server/remote_esp32.py
                                            │
                                    monitor serial siempre activo
                                    (esp_idf_monitor + ELF para backtraces)
```

---

## 1. Estructura de archivos

```
remoteFlashServer/
├── common.py                  # Utilidades compartidas: SHA256, send_msg, recv_msg
├── requirements.txt           # Dependencias Python: esptool, esp-idf-monitor
├── install.sh                 # Script de instalación en la Pi (como root)
│
├── client/
│   └── deploy.py              # Cliente: build opcional + empaqueta ZIP + envía TCP
│
├── server/
│   ├── remote_esp32.py        # Entrypoint delgado: señales, logging, arranque
│   ├── protocol.py            # Servidor TCP: acepta conexiones, orquesta el flash
│   ├── flash.py               # Funciones esptool: find, build_cmd, run_cmd
│   └── monitor.py             # EspMonitor: esp_idf_monitor via PTY, buffer circular
│
├── infra/
│   ├── devremote              # CLI para gestionar sesiones tmux por dispositivo
│   ├── devremote.service      # Systemd unit (oneshot) que lanza devremote al boot
│   ├── esp32_tmux.sh          # Script auxiliar llamado por udev al conectar un USB
│   └── 99-esp32.rules         # Regla udev: lanza esp32_tmux.sh al detectar ttyUSB*
│
├── tests/
│   ├── test_common.py
│   ├── test_flash.py
│   └── test_artifact.py
│
└── issues/                    # Historial de issues del proyecto
```

---

## 2. Módulos

### `common.py`

Utilidades compartidas entre cliente y servidor. Importado por `deploy.py`, `protocol.py` y los tests.

- `sha256_file(p)` — calcula SHA256 de un archivo en bloques de 1 MB.
- `send_msg(sock, obj)` — serializa un dict como JSON y lo envía con framing de 4 bytes big-endian.
- `recv_msg(sock)` — recibe y deserializa un mensaje con el mismo framing.

### `server/flash.py`

Funciones puras de flasheo con esptool. Sin estado, sin hilos.

- `find_esptool_cmd()` — busca `esptool.py` en PATH o como módulo Python (`python -m esptool`). Lanza `RuntimeError` si no lo encuentra.
- `build_esptool_cmd(esptool_cmd, chip, port, baud, encrypt, erase, jobdir)` — lee `flasher_args.json` del directorio del job, resuelve los pares `(offset, archivo)` con varios fallbacks (dict, list, nombres por defecto, glob `*.bin`), y construye los comandos `erase-flash` y `write-flash` para esptool.
- `run_cmd(cmd, log)` — ejecuta un subproceso, imprime su salida línea a línea y retorna el código de retorno.

### `server/monitor.py`

Monitor serial basado en `esp_idf_monitor`. Contiene la clase `EspMonitor` y la lógica de erase region interactivo.

**`EspMonitor`:**
- Lanza `python -m esp_idf_monitor` en un PTY. Si existe `current.elf`, lo pasa con `--elf` para decodificación de backtraces.
- El hilo `_pump` hace relay bidireccional: salida del monitor → stdout + logfile diario; teclado → monitor.
- Intercepta `Ctrl-C` (termina el servidor) y `Ctrl-E` (lanza `erase_region_interactive` en hilo separado).
- Mantiene un buffer circular de 64 KB de salida reciente para que `erase_region_interactive` pueda parsear la tabla de particiones.
- `_ignore_signals_flag` (threading.Event) compartido con `remote_esp32.py` para inhibir SIGTERM/SIGINT durante operaciones temporales.

**`erase_region_interactive`:**
- Parsea la tabla de particiones del buffer circular del monitor.
- Permite seleccionar particiones por número o ingresar offset/tamaño manual.
- Detiene el monitor, ejecuta `esptool erase_region`, reinicia el monitor.

### `server/protocol.py`

Servidor TCP de control. Importado por `remote_esp32.py`.

- `control_server(cfg, mon, svc_log)` — loop de `accept()`. Por cada conexión, llama a `handle_control` en el mismo hilo (conexiones serializadas).
- `handle_control(sock, cfg, mon, svc_log)` — orquesta el ciclo completo: recibir header JSON → validar token → ACK → recibir artefacto (upload o pull desde URL) → verificar SHA256 → extraer ZIP → copiar `firmware.elf` → detener monitor → flashear → responder → relanzar monitor.

### `server/remote_esp32.py`

Entrypoint del servidor. Delgado por diseño.

- Parsea argumentos CLI: `--port-tty`, `--serial-baud`, `--control-port`, `--chip`, `--flash-baud`, `--token`, `--base`.
- Configura logging dual (stdout + archivo `remote_esp32.service.log`).
- Instancia `EspMonitor` y lo arranca.
- Lanza `control_server` en un hilo daemon.
- Espera `_shutdown_flag` (seteado por los signal handlers de SIGTERM/SIGINT/SIGHUP).

### `client/deploy.py`

Cliente de flash. Corre en la máquina del desarrollador.

**Modos de operación:**
```
--mode local   → idf.py build + idf.py (encrypted-)flash + idf.py monitor
--mode remote  → idf.py build + collect_artifact() → ZIP → TCP → espera resultado
--mode auto    → detecta configuración remota → elige automáticamente
```

**`collect_artifact()`:** lee `flasher_args.json` del build dir, arma `artifact.zip` con los binarios y sus offsets. Opcionalmente incluye `firmware.elf` para el pipeline de backtraces.

**Modo `--custom`:** selector de archivos nativo por OS (osascript en macOS, PowerShell en Windows, tkinter en Linux) para flashear binarios de otro proyecto. Persiste la selección en `.custom_flash_files.json`.

**Retry automático:** si el flash falla y hubo build previo, pregunta si reintentar sin build.

**Configuración:** `.flashcfg.json` (gitignoreado), creado manualmente por el usuario:
```json
{
  "mode": "auto",
  "paths": { "project_root": ".", "idf_py": "idf.py" },
  "local":  { "port": "/dev/ttyUSB0", "monitor": true },
  "remote": { "host": "192.168.1.100", "port": 5000, "token": "secret" },
  "chip": "esp32",
  "flash_baud": 921600,
  "encrypt": true,
  "erase": false
}
```

---

## 3. Flujo de flash remoto

```
deploy.py                              protocol.py / remote_esp32.py
   │
   ├─ 1. idf.py build  (opcional)
   ├─ 2. collect_artifact()
   │      └─ ZIP: flasher_args.json + *.bin [+ firmware.elf]
   │
   ├─ 3. TCP connect → send_msg(header JSON)
   │      { token, action, job_id, chip, baud, encrypt, erase,
   │        artifact_size, artifact_sha256 }
   │                                        │
   │                                        ├─ 4. validar token
   │                                        ├─ 5. crear jobdir
   │◄────────────────── ACK { ok, phase:"ready", job_id } ───┤
   │
   ├─ 6. enviar bytes del ZIP (streaming)
   │                                        │
   │                                        ├─ 7. verificar SHA256
   │                                        ├─ 8. extraer ZIP en jobdir/
   │                                        ├─ 9. copiar firmware.elf → current.elf
   │                                        ├─ 10. _ignore_signals_flag.set()
   │                                        ├─ 11. mon.stop()
   │                                        ├─ 12. esptool write_flash (retry sin --encrypt si rc==2)
   │◄────────────── result JSON { ok, job_id, write_rc, ... } ┤
   │                                        ├─ 13. mon.start()
   │                                        └─ 14. _ignore_signals_flag.clear()
```

El monitor se detiene solo después de recibir el artefacto completo y verificado (paso 11), minimizando el tiempo sin salida serial.

---

## 4. Pipeline `.elf`

El archivo `firmware.elf` viaja dentro del ZIP como artefacto opcional. Su propósito es habilitar la decodificación de backtraces en `esp_idf_monitor`.

```
[Máquina del desarrollador]
  build/firmware.elf
       │
       └─► collect_artifact() lo incluye en artifact.zip
                │
                │  TCP
                ▼
[Raspberry Pi]
  jobdir/firmware.elf
       │
       └─► shutil.copy2 → /opt/esp/current.elf
                                  │
                                  └─► EspMonitor.start()
                                        cmd: python -m esp_idf_monitor
                                             --port /dev/ttyUSBX
                                             --baud 115200
                                             --elf /opt/esp/current.elf
```

`current.elf` se sobreescribe en cada flash. `EspMonitor` verifica en cada `start()` si el archivo existe antes de pasarlo con `--elf`, por lo que el primer arranque (sin ELF aún) funciona sin decodificación de backtraces.

---

## 5. Infraestructura

### Paths en la Pi (instalados por `install.sh`)

```
/opt/esp/
├── server/               ← copia de server/ del repo
│   ├── remote_esp32.py
│   ├── protocol.py
│   ├── flash.py
│   └── monitor.py
├── common.py             ← copia de common.py del repo
├── logs/                 ← logs del servicio y serial diario
│   ├── remote_esp32.service.log
│   └── YYYYMMDD/
│       ├── serial.log
│       └── job_YYYYMMDD_HHMMSS.log
├── jobs/                 ← directorio de trabajo por job (ZIPs extraídos)
│   └── job_YYYYMMDD_HHMMSS/
│       ├── artifact.zip
│       ├── flasher_args.json
│       ├── firmware.elf
│       └── *.bin
└── current.elf           ← último ELF flasheado (para backtrace decoding)

/usr/local/bin/
├── devremote             ← CLI de gestión de sesiones tmux
└── esp32_tmux.sh         ← script auxiliar llamado por udev

/etc/udev/rules.d/
└── 99-esp32.rules        ← lanza esp32_tmux.sh al conectar ttyUSB*

/etc/systemd/system/
└── devremote.service     ← arranca devremote al boot
```

### `devremote` (CLI)

Script Bash que gestiona una sesión tmux por cada `/dev/ttyUSBX` presente.

- Sin argumentos: detecta dispositivos y arranca sesiones faltantes.
- `--reset`: mata todas las sesiones `esp32_*` y las reinicia.
- `--status`: muestra estado (RUNNING/DOWN), puerto TCP y PID por dispositivo.
- `<N>`: hace `tmux attach-session` a la sesión del `ttyUSBN`.

Cada sesión se llama `esp32_ttyUSBX` y escucha en el puerto `5000 + X`.

### `esp32_tmux.sh`

Script auxiliar lanzado directamente por la regla udev al detectar un `ttyUSB*` nuevo. Crea la sesión tmux para ese dispositivo si no existe.

### Regla udev `99-esp32.rules`

```
ACTION=="add", SUBSYSTEM=="tty", KERNEL=="ttyUSB*", \
RUN+="/usr/bin/tmux new-session -d -s esp32_%k '/usr/local/bin/esp32_tmux.sh /dev/%k'"
```

Permite que los dispositivos ESP32 levanten su sesión automáticamente al conectarse.

### Servicio systemd `devremote.service`

Unit de tipo `oneshot` con `RemainAfterExit=yes`. Corre `devremote` al boot para arrancar las sesiones de los dispositivos ya conectados. Complementa la regla udev (que cubre conexiones en caliente).

---

## 6. Instalación

```bash
sudo ./install.sh
```

El script es idempotente. Realiza los siguientes pasos:

1. Verifica que `tmux`, `python3` y `pip3` estén disponibles.
2. Crea `/opt/esp/server/`, `/opt/esp/logs/`, `/opt/esp/jobs/`.
3. Copia `server/*` → `/opt/esp/server/`.
4. Copia `common.py` → `/opt/esp/common.py`.
5. Instala `infra/devremote` → `/usr/local/bin/devremote` (ejecutable).
6. Instala `infra/esp32_tmux.sh` → `/usr/local/bin/esp32_tmux.sh` (ejecutable).
7. Instala `infra/99-esp32.rules` → `/etc/udev/rules.d/`.
8. Instala `infra/devremote.service` → `/etc/systemd/system/`.
9. Recarga reglas udev (`udevadm control --reload-rules && udevadm trigger`).
10. Habilita el servicio (`systemctl enable devremote`), sin iniciarlo.

Al finalizar muestra un resumen de rutas instaladas. Para iniciar el servicio manualmente: `systemctl start devremote`.

---

## 7. Dependencias

| Herramienta | Usado por | Cómo se localiza |
|---|---|---|
| `esptool` | `flash.py` (servidor) | `shutil.which("esptool.py")` → `python -m esptool` → `RuntimeError` |
| `esp-idf-monitor` | `monitor.py` (servidor) | `python -m esp_idf_monitor` (debe estar en el mismo entorno Python) |
| `idf.py` | `deploy.py` (cliente, build local) | PATH → `IDF_PATH`/`ESP_IDF_PATH` → rutas comunes |
| `tmux` | `devremote`, `esp32_tmux.sh` | debe estar en PATH del servidor |
| `osascript` | `deploy.py` (macOS, modo --custom) | sistema |
| `powershell` | `deploy.py` (Windows, modo --custom) | sistema |
| `tkinter` | `deploy.py` (Linux, modo --custom) | stdlib Python |

Las dependencias Python del servidor se declaran en `requirements.txt`:

```
esptool
esp-idf-monitor
```

Instalar con: `pip install -r requirements.txt`

---

## 8. Norte futuro

El siguiente paso natural es un **dashboard web** que permita ver los logs seriales en tiempo real desde el browser, sin necesidad de hacer `devremote <N>` para adjuntarse a la sesión tmux.

Arquitectura propuesta:

```
[Browser]  ◄──── WebSocket ────  [Servidor HTTP/WS en la Pi]
                                          │
                                  suscribe al buffer circular
                                  de EspMonitor (ya existe)
                                          │
                                  también expone endpoint REST
                                  para consultar estado y jobs
```

El buffer circular de 64 KB que `EspMonitor` ya mantiene es la base natural para esto: el servidor WS puede transmitir el contenido existente al conectar un cliente nuevo y luego hacer streaming de los nuevos bytes conforme llegan.

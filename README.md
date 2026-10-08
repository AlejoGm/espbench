# espbench

Remote ESP32 firmware deployment system. Build on your dev machine, flash to an ESP32 connected to a Raspberry Pi over TCP. Includes a persistent serial monitor and web dashboard.

**Version:** 0.47.1

---

## Architecture

```
Dev machine                          Raspberry Pi
────────────                         ────────────────────────────────────────────
client/deploy.py ──── TCP ────►  remote_esp32.py   (one process per device, in tmux)
  │                                 ├─ Device (FSM) + DeviceLog
  │  1. idf.py build (optional)     ├─ EspMonitor (esp_idf_monitor + ELF backtraces)
  │  2. zip firmware artifact       └─ control server (port 5000+N)
  │  3. send over TCP                       │ writes devices/<mac>/, run/<tty>.json
  │  4. receive result                      ▼
  └─ .flashcfg.json               api.py (dashboard, FastAPI, port 8080)
     (local gitignored config)      └─ /api/devices, /ws/device/{tty}
```

One tmux session per device. Device name and TCP port come from `espbench-name`: `ttyUSBN` → `5000+N`, or a stable `esp-slotK` → `5000+K` if the physical USB port is mapped (see [Stable ports](#stable-ports-optional)). Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Fresh Pi Setup

Complete steps for a new Raspberry Pi.

### 1. Flash OS

Raspberry Pi OS Lite 64-bit (Bookworm). Enable SSH and set hostname via Raspberry Pi Imager advanced settings.

### 2. First SSH — install base packages

```bash
sudo apt update && sudo apt install -y git tmux python3 python3-pip python3-venv
```

### 3. Clone repo

The clone lives in `/opt/espbench`, owned by root: `install.sh` records it in `/opt/esp/update.conf` and the
bench updates itself from there (see [Update](#update-existing-pi)). Nobody runs git by hand on the bench.

```bash
sudo git clone https://github.com/AlejoGm/espbench.git /opt/espbench
cd /opt/espbench
```

To run a branch instead of the latest release, clone it with `-b <branch>`: the install pins the bench to it.

### 4. System hardening (optional but recommended)

Disables desktop, serial TTL, HID, installs WiFi provisioning AP fallback and Tailscale:

```bash
sudo bash rpi/pi-setup.sh
```

Pass a Tailscale auth key so the bench joins the tailnet as a **tagged node** (`tag:bench`), not as your user
(single-use, non-ephemeral key with `tag:bench`; see [docs/security.md](docs/security.md)):

```bash
sudo TS_AUTHKEY=tskey-auth-... bash rpi/pi-setup.sh
```

Without `TS_AUTHKEY`, join later with `sudo tailscale up --auth-key=<key> --advertise-tags=tag:bench`.

Then reboot to apply all changes:

```bash
sudo reboot
```

### 5. Install server

```bash
sudo bash remote/install.sh
```

Installs server files to `/opt/esp/`, creates Python venv, registers udev rules and systemd services.

### 6. Start services

```bash
sudo systemctl start devremote dashboard
```

`devremote` manages tmux sessions per device. `dashboard` exposes the web UI on port 8080.
Plug in an ESP32 via USB — udev auto-creates a session for it.

### 7. Verify

```bash
devremote --status
```

Dashboard: `http://<pi-hostname>:8080`

---

## Update (existing Pi)

Los benches siguen los **releases** (tags `vX.Y.Z`) solos: `espbench-update.timer` corre `espbench-update --auto`
3 min después del boot y cada noche a las 04:00 (± 20 min). Instala el último release, reinicia `dashboard` y
resetea las sesiones; si después el dashboard no contesta `/api/version` con la versión nueva, **vuelve al commit
anterior**. No toca un bench ocupado (placa flasheando/borrando o con reserva vigente): reintenta la próxima vez.

```bash
sudo espbench-update                    # ahora: al PIN si hay, si no al último release
sudo espbench-update --ref feat/x       # probar una rama/tag/commit: queda fijo ahí (PIN), el automático no lo toca
sudo espbench-update --release          # volver a seguir los releases
cat /opt/esp/update_status.json         # resultado del último update (también GET /api/update)
tail -50 /opt/esp/update.log
```

Desde bench-master: botón **⟳ update** en cada bench (`POST /api/update`).

**Publicar un release** (lo toman todos los benches sin PIN esa noche):

```bash
git switch main && git pull && git tag v$(cat VERSION) && git push origin v$(cat VERSION)
```

`remote/infra/update.sh` sigue andando desde el clone (`sudo bash remote/infra/update.sh [ref]`): es un atajo de
`espbench-update` que, sin argumentos, actualiza la rama en la que está el clone.

---

## Quick Start

### Client (Dev machine)

```bash
cd client && ./install.sh
```

Create `.flashcfg.json` in your ESP-IDF project root. The easiest way: open the dashboard, click **⎘ Config** on a device card, and paste the copied snippet into your config file, then add the missing fields:

```json
{
  "mode": "auto",
  "paths": { "project_root": ".", "idf_py": "idf.py" },
  "remote": [
    {
      "name": "mi-board",
      "host": "sensipi03",
      "token": "",
      "lock_user": "yourname",
      "lock_token": "your_lock_token"
    }
  ],
  "chip": "esp32",
  "flash_baud": 921600
}
```

Flash:

```bash
python client/deploy.py
```

---

## Uso por agentes (`espbench`)

CLI para que un agente (Claude Code) cierre el ciclo contra una placa real: flash, verificar el arranque, mandar
comandos por la consola serie, esperar la respuesta, leer log y eventos. Salida `--json` (un objeto por comando) y
exit codes por causa. Spec: [docs/specs/agents-cli.md](docs/specs/agents-cli.md) §8.

```bash
cd client && ./install.sh          # deja `espbench` en ~/.local/bin (ESPBENCH_BIN_DIR para otro lugar)

espbench benches --json                  # benches encontrados (Tailscale + ~/.config/espbench-benches.json)
espbench ls --json                       # sin host: las placas de todos los benches, con `bench` y `location`
espbench ls --location chile             # solo los benches cuya ubicación ("Santiago, CL") dice "chile"/"cl"
espbench pick --where chip=esp32-s3 --reserve --ttl 30m --json   # la primera libre que cumple, ya reservada (no una mía: --include-mine)
espbench note mi-board "agente: probando OTA" --json             # aviso para otros (--clear al terminar)
espbench set mi-board chip=esp32-s3 conectividad+=lte estado=    # propiedades (estado= la quita)
espbench props --json                    # categorías y valores (con varios benches, la unión y dónde está cada valor)
espbench props add conectividad nb-iot --bench bench-chile      # valor nuevo: el catálogo es de cada bench
espbench reserve mi-board --ttl 30m --json
idf.py build && espbench flash mi-board --verify --json        # flash + primer boot + 10 s sin crash
espbench send mi-board "status" --until "OK" --json
espbench events mi-board --type panic --json
espbench logs mi-board --around panic --max-lines 80 --json
espbench release mi-board --json
```

| Exit | `error` |
|---|---|
| 0 | ok |
| 1 | `bad_request` / `unexpected` / `in_use` / `unsupported` |
| 2 | `flash_failed` |
| 3 | `crashed` (panic, boot loop o reinicio en la ventana) |
| 4 | `timeout` |
| 5 | `busy` |
| 6 | `locked` / `reservation_lost` / `token_mismatch` |
| 7 | `not_found` / `ambiguous` / `device_changed` / `session_down` |
| 8 | `bad_anchor` / `cursor_expired` |
| 9 | `session_ended` |
| 10 | `network` / `auth` / `auth_config` |

**Config** (gana la primera): flags (`--host`, `--profile`) > env (`ESPBENCH_HOST`, `ESPBENCH_TOKEN`, `ESPBENCH_USER`,
`ESPBENCH_LOCK_TOKEN`) > `~/.config/espbench.json` > `remote` del `.flashcfg.json` del proyecto (host, token,
lock_user, lock_token; chip, `flash_baud` y `encrypt` también salen de ahí).

```json
{"default_profile": "lab",
 "profiles": {"lab": {"host": "sensipi03", "token": "", "lock_user": "alejo-agent", "lock_token": "..."}}}
```

`host` acepta `host`, `host:puerto` (default 8080, el dashboard) o una URL. El flash va por TCP al puerto de la
placa, que el CLI saca de `/api/devices`.

**Sin host** (o `"host": "auto"`) el CLI encuentra los benches solo, como `deploy.py` y bench-master: peers online de
Tailscale + `~/.config/espbench-benches.json`. `ls` lista las placas de todos (`--bench <nombre>` para uno) y cada
comando busca `<dev>` en todos; si está en dos, `ambiguous` con dónde: `<dev>@<bench>` o `--bench` para elegir.
La lista de benches se cachea 30 s (`~/.cache/espbench/benches.json`). Los benches **muy** viejos (sin `app: espbench`
en `/api/version`, p. ej. 0.6.x) se ignoran; los que sí son espbench pero anteriores a las propiedades (0.33–0.35)
se usan para todo, y `note` / `set` / `props` contra ellos dan `unsupported` ("actualizalo").

**Token de la API**: uno solo (`ESPBENCH_TOKEN` / perfil) para todos los benches. Si cada bench tiene el suyo,
`~/.config/espbench-benches.json` acepta `"tokens": {"<bench>": "<token>"}` (un `ESPBENCH_TOKEN` / `--token`
explícito gana). `pick --reserve` saltea un bench que rechaza el token (`skipped`, con el `bench`).

**Skill para Claude Code** (`client/agent/SKILL.md`: cuándo usar `espbench`, el ciclo, qué hacer con cada `error`,
cómo no llenar el contexto de log):

```bash
mkdir -p ~/.claude/skills && ln -sfn "$PWD/client/agent" ~/.claude/skills/espbench    # desde la raíz del repo
```

`ls --json` marca `available: true` en las placas libres o con lock propio y sin `estado` excluido (`no-tocar`,
`roto`: `avoid: true`); `--where cat=valor` (repetible, AND) y `--free` filtran. `pick` hace eso solo: la primera
placa libre, sana, que cumple los `--where`, en cualquier bench (las que tienen nota, al final), y con `--reserve`
la reserva. **Nota** (`note`): texto libre, un aviso para personas y agentes, no un lock. **Propiedades** (`props`):
categorías fijas (`estado`, `uso`, `chip`, `conectividad`) con valores que cada bench puede ampliar
(`espbench props add conectividad nb-iot`, o desde el dashboard); ver ARCHITECTURE §8 "Nota y propiedades".

Probar sin Pi: `ESP_BASE=$(mktemp -d) python -m tests.benchsim --port 8099` levanta una Pi simulada (API real, una
placa que bootea, contesta `status` y crashea con `panic`); después `ESPBENCH_HOST=127.0.0.1:8099 espbench ls`.

---

## Dashboard

Web UI at `http://<pi-ip>:8080`, light or dark theme: the bench (temperature, RAM, disk, load, uptime), each board with its status, uptime, last log and 24 h activity, and a live serial monitor with events, history and console.

Escrituras, token de la API y cómo liberar una placa: ver [Despliegue y seguridad](#despliegue-y-seguridad).

---

## Despliegue y seguridad

El banco está pensado para una **red interna de confianza**. Sin `/opt/esp/api_token` (el default) queda abierto:

| Qué | Sin `api_token` | Con `api_token` |
|---|---|---|
| Lecturas: `/api/devices`, `/api/board/{key}/log\|events`, WebSocket del log, historial y sesiones | abiertas | **abiertas igual** (a propósito) |
| `send`, `command` (reset / bootloader), `restart-session` | cualquiera en la red | `Authorization: Bearer <token>` |
| `reserve`, `release`, `unlock` con el par `lock_user`/`lock_token` | cualquiera que tenga el par (o cree uno nuevo si la placa está libre) | + Bearer |
| `unlock` forzado (botón **Forzar** del dashboard) | deshabilitado (403 `force_disabled`) | Bearer |
| Flash (TCP `5000+K`) | sin auth: alcanza un par `lock_user`/`lock_token` | `remote.token` del `.flashcfg.json` = el token |

- **Las lecturas no piden token nunca**: el log serie, los eventos y el texto de cada `send` de la consola los ve
  cualquiera en la red. No mandes secretos por la consola serie.
- **La reserva es de buena fe para `send`/`command`**: bloquea a los demás (423 `locked`), pero el dashboard la
  saltea con `force: true` después de confirmar, y eso no pide token (queda `forced` y quién en el evento). Lo que
  sí protege siempre es el **flash**: con una reserva o un lock ajeno, el flash da `device_locked`. El CLI nunca
  manda `force`.
- Una reserva dura como mucho **24 h** (`ttl_s` mayor → 400 `bad_request`; para más, renovarla con otro
  `reserve`). El lock que deja un flash no vence: lo suelta su dueño o se borra al relanzar la sesión.

### Activar el token

El mismo archivo es el token de las escrituras del API **y del flash** (salvo que la sesión corra con `--token`):
apenas existe, todo `.flashcfg.json` sin `remote.token` (o con otro) **deja de poder flashear** (`unauthorized`).
Por eso el orden importa:

1. Elegí el token y **repartilo primero**: `remote.token` en el `.flashcfg.json` de cada proyecto y
   `ESPBENCH_TOKEN` (o `token` del perfil) de cada agente. Mientras la Pi no tenga el archivo, el token de más se ignora.
2. **Recién después** crealo en la Pi. El api corre como `sfypi` y los procesos de las placas como root:

   ```bash
   sudo sh -c 'umask 027; printf "%s\n" "<token>" > /opt/esp/api_token'
   sudo chown root:sfypi /opt/esp/api_token && sudo chmod 640 /opt/esp/api_token
   ```

   Se lee en cada pedido y en cada conexión de flash: no hace falta reiniciar nada. Si existe y no se puede leer
   (permisos, no es UTF-8), **falla cerrado**: las escrituras dan 500 `auth_config` y el flash `auth_config`.
3. El dashboard pide el token una vez (ante el primer 401) y lo guarda en el navegador; con el token aparece **Forzar**.

Para volver a sin auth: borrar el archivo (o dejarlo vacío).

### Liberar una placa trabada

| Quién | Cómo |
|---|---|
| El dueño (con su par) | `espbench release <dev>`, `python client/deploy.py --unlock`, o **Liberar** en el dashboard |
| Cualquiera, con `api_token` | **Forzar** en el dashboard (queda un `release` con el dueño anterior y quién forzó) |
| Desde la Pi | `devremote --unlock <dev>`: suelta cualquier lock o reserva y deja un evento `release` (forzado, `by_host: devremote`) |
| Solo | una reserva vence sola (máx. 24 h); el lock de un flash se borra al relanzar la sesión (replug, `devremote --reset`) |

---

## bench-master (all benches in one place)

A *bench* is any host running the dashboard (a Pi or another machine). `bench-master` runs **on your dev machine**,
finds every bench and shows all their devices in one grid, with each bench's monitor/console proxied through it:

```bash
master/bench-master --open      # first run creates master/.venv; http://localhost:8090
```

- **Discovery**: online Tailscale peers that answer `GET :8080/api/version` as espbench, plus hosts listed in
  `~/.config/espbench-benches.json`: `{"hosts": ["10.0.0.5", "lab:8080"], "tailscale": true}`.
- A bench names itself: `/opt/esp/bench_name` on the bench, or its hostname.
- A bench knows **where it is** at city level ("Santiago, CL"), by geolocating its public IP. bench-master shows it and
  can group by it; `espbench benches` shows it and `espbench ls --location` filters by it. A manual override (e.g. the
  bench exits through a VPN in another city) is set from the bench dashboard header (`PATCH /api/bench {location}`);
  clearing it goes back to the automatic one.
- **Privacy**: once a day (and when the dashboard starts) the bench sends a request with its public IP to
  `https://ipinfo.io/json` (fallback `https://ipapi.co/json/`), 3 s timeout, no token. To disable it:
  `sudo touch /opt/esp/geo_disabled` (then only the manual location is shown). Stored in `/opt/esp/meta/bench_geo.json`.
- A bench that stops answering stays listed as offline with its last known devices.
- Listens on 127.0.0.1 only and rejects cross-site requests: the proxy gives access to every bench's serial console.

Details: [master/CLAUDE.md](master/CLAUDE.md), [docs/ARCHITECTURE.md §12](docs/ARCHITECTURE.md), test guide: [docs/BENCH_MASTER_TESTING.md](docs/BENCH_MASTER_TESTING.md).

---

## Configuration Reference (`.flashcfg.json`)

| Field | Description |
|-------|-------------|
| `mode` | `local` \| `remote` \| `auto` \| `custom` |
| `paths.project_root` | ESP-IDF project root |
| `paths.idf_py` | Path to `idf.py` (optional, auto-detected) |
| `local.port` | Serial port for local flash |
| `local.monitor` | Open monitor after flash |
| `remote.host` | Pi IP or hostname. Omit it (or `"auto"`) to find the bench the device is on: `name` is then the device_key, SN, MAC or `<bench>/<tty>` |
| `remote.port` | TCP port (`5000 + device index`) |
| `remote.token` | Server auth token (= `/opt/esp/api_token` on the Pi, if it exists) |
| `remote.lock_user` | Username for device locking |
| `remote.lock_token` | Token for device locking (may contain `:` again: it is escaped in the lock file) |
| `chip` | ESP chip model (`esp32`, `esp32s3`, etc.) |
| `flash_baud` | Flash baud rate |
| `encrypt` | Flash encryption enabled. The generated template takes it from `CONFIG_SECURE_FLASH_ENC_ENABLED` in `sdkconfig` |
| `erase` | Erase flash before writing |

---

## Project Structure

```
espbench/
├── common.py                  # Shared: TCP framing, SHA256, MAC↔SN, HW model utils
├── client/
│   ├── deploy.py              # CLI for humans: build + artifact + remote/local flash
│   ├── espbench.py            # CLI for agents (`espbench`, --json, exit codes)
│   ├── espbench_lib.py        # Client library: config, HTTP API, log ranges with waits, flash + verify
│   └── agent/SKILL.md         # Claude Code skill for `espbench`
│   └── benches.py             # Bench discovery (Tailscale + config) and device → bench resolve
├── master/                    # bench-master: all benches in one place (runs on the dev machine)
├── remote/
│   ├── server/
│   │   ├── remote_esp32.py    # Per-device process: wiring, MAC discovery, threads
│   │   ├── device.py          # TtyPort / Device (FSM) / DeviceManager
│   │   ├── device_log.py      # DeviceLog: the device's log (single writer)
│   │   ├── monitor.py         # EspMonitor: esp_idf_monitor in a PTY
│   │   ├── protocol.py        # TCP flash protocol, split into testable phases
│   │   ├── erase.py           # Interactive Erase Region (Ctrl-E)
│   │   ├── flash.py           # esptool: find, build command, run, read MAC
│   │   ├── api.py             # Dashboard backend: REST + WebSocket
│   │   ├── device_registry.py # Dashboard's read model of the devices
│   │   ├── log_streamer.py    # Log tail → WebSocket
│   │   ├── runstate.py        # run/<tty>.json (runtime state between processes)
│   │   ├── paths.py           # Every path under ESP_BASE
│   │   └── taglog.py          # taglog.info(TAG, msg) logging
│   ├── dashboard/             # Web UI: index.html, device.html, style.css
│   └── infra/                 # devremote, esp32_tmux.sh, espbench-name, udev, systemd
├── tests/                     # pytest suite (host, no hardware)
├── docs/                      # ARCHITECTURE.md; archive/ = old PRDs and code
├── rpi/                       # Pi bootstrap script
└── scripts/                   # Maintenance scripts
```

---

## Flash Workflow (Remote)

1. `deploy.py` reads `.flashcfg.json`
2. Optionally runs `idf.py build`
3. Zips `flasher_args.json` + `*.bin` + `firmware.elf` → `artifact.zip`
4. TCP connect to Pi → send header (token, chip, job metadata, lock user/token)
5. Pi checks token, device state (rejects with `device_busy` during an erase) and lock, then ACKs
6. Upload artifact, Pi verifies SHA256
7. Pi: device → FLASHING, stops monitor → `esptool write_flash` (retries without `--encrypt` on rc=2) → restarts monitor → back to MONITORING
8. Client receives result JSON (esptool output is streamed live while flashing), sent once the monitor is back up; it carries the `cursor` of the `flash` event in the device log

---

## Server Paths (Raspberry Pi)

```
/opt/esp/
├── server/, dashboard/, venv/    code + Python env
├── devices.json                  MAC → device_key + hw_model
├── slots.conf                    (optional) stable ports: <K> <ID_PATH>
├── run/<tty>.json                runtime state of each session (state, MAC, port, log path)
├── devices/<MAC>/
│   ├── output.log                current session log (serial + server events)
│   ├── output_<ts>.log           previous sessions
│   ├── current.elf               last flashed ELF (backtrace decoding)
│   ├── last_user
│   └── jobs/<job_id>/            extracted artifact + job.log
├── locks/<tty>                   device lock (user:token[:expires[:mac]])
├── api_token                     (optional) API + flash token
├── update.conf                   REPO_DIR (the clone, /opt/espbench) + PIN (empty = follow releases)
├── update_status.json, update.log  last espbench-update
├── bench_name                    (optional) bench name for bench-master
├── meta/                         777, written by the dashboard: properties.json, bench_geo.json, bench_location
├── geo_disabled                  (optional) don't geolocate the bench by its public IP
└── VERSION
```

Devices whose MAC can't be read keep the per-tty layout (`devices/unknown-<tty>/`, `jobs/`, `current_<tty>.elf`).

---

## Tests

```bash
pytest tests/
```

No hardware required. The protocol, the per-device entrypoint and the infra shell scripts run end-to-end against fakes at the edges (esptool, esp_idf_monitor, socket, tmux, udev). What can only be verified on a real Pi is the prioritized checklist in [docs/PI_CHECKLIST.md](docs/PI_CHECKLIST.md) (P0 deploy → P3 infra): run it after every big update.

---

## Device Management (Pi)

```bash
devremote                 # start missing sessions
devremote --status        # device / kernel tty / port / session / FSM state / pid
devremote 0               # attach to a device's session (0 = ttyUSB0; also esp-slotK, slotK)
devremote --reset         # restart all sessions
devremote --reset 0       # restart one device
devremote --unlock 0      # release a device lock (any owner; leaves a `release` event)
devremote --slots         # ID_PATH of each physical port (for slots.conf)
devremote --cleanup       # delete old jobs and rotated logs (--dry-run to preview); their events stay
                          # in events.jsonl, so `--around <cursor>` of a deleted session → cursor_expired
```

### Stable ports (optional)

`ttyUSBN` follows the kernel's enumeration order, not the physical port: a replug or reboot can swap them, and with them the TCP port your `.flashcfg.json` points to. To pin ports to physical hub slots:

```bash
devremote --slots                                  # shows each port's ID_PATH
sudo nano /opt/esp/slots.conf                      # one line per port: "<K> <ID_PATH>"
sudo udevadm trigger --subsystem-match=tty && devremote --reset
```

Mapped devices become `esp-slotK` on port `5000+K`. Without `slots.conf` nothing changes. With it, unmapped devices get `5100+N` so they don't collide with slot ports.


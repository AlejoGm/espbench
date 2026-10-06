# client/

Developer-side deploy tool. Runs on dev machine to build ESP-IDF firmware and flash it locally or to a remote Pi.

## Entry Point

`deploy.py` — 1040 lines, monolithic CLI

## Modes

| Mode | Behavior |
|------|----------|
| `local` | Run esptool directly on local `/dev/ttyUSBx` |
| `remote` | TCP to Pi server, upload artifact, remote flash |
| `auto` | Detect from `.flashcfg.json` |
| `custom` | File picker dialog to flash binaries from another project |

Config in `.flashcfg.json` (gitignored, user-created per project).

## Build dir

`--build-dir` (default `build`) selects which build dir to compile and flash; relative paths resolve
against `project_root`. It is threaded into every `idf.py` call (`-B`), so a project that builds
several products into separate dirs can flash a specific one instead of whatever is left in `build/`.

## Flash Config Schema (`.flashcfg.json`)

```json
{
  "mode": "auto",
  "paths": { "project_root": ".", "idf_py": "idf.py" },
  "local":  { "port": "/dev/ttyUSB0", "monitor": true },
  "remote": {
    "host": "192.168.1.x",
    "port": 5000,
    "token": "secret",
    "lock_user": "developer",
    "lock_token": "lock_secret"
  },
  "chip": "esp32",
  "flash_baud": 921600,
  "encrypt": true,
  "erase": false
}
```

## Benches (`benches.py`)

Encuentra los benches (hosts con el dashboard de espbench en :8080, Pi u otra máquina) y en cuál está cada placa.
Solo stdlib: lo usan `deploy.py`, bench-master (`master/`) y el CLI de agentes.

- Candidatos: peers **online** de `tailscale status --json` + `hosts` de `~/.config/espbench-benches.json`
  (`ESPBENCH_BENCHES_CONFIG` para otra ruta): `{"tailscale": true, "hosts": ["10.0.0.5", "lab:8080"], "timeout_s": 2}`.
- Es bench si `GET /api/version` devuelve `app: "espbench"` (o solo `{"version"}`, benches sin actualizar).
  El nombre lo declara el bench (`name`); dos caminos al mismo bench cuentan una vez.
- `resolve(key)`: key = device_key, SN, MAC o `<bench>/<tty>`. `ResolveError` si no está o si está en más de un bench.

En `.flashcfg.json`, un remote **sin `host`** (o `"host": "auto"`) se resuelve así: `{"name": "medidor-a", "lock_user": ..., "lock_token": ...}`.
Un solo scan por corrida (`_benches_cache`).

## Key Functions

- `find_idf_py()` — locates idf.py via config or PATH
- `auth_ping()` — checks remote server reachability
- `collect_artifact()` — zips `flasher_args.json` + `*.bin` + `firmware.elf`
- `select_custom_files_*()` — OS-specific file picker (macOS osascript, Windows PowerShell, Linux tkinter)
- Retry logic: on flash fail with prior build, offers retry without rebuild

## Output

Uses `rich` library for formatted terminal output. Fallback to plain text if unavailable.

## Dependencies

```
rich>=13.0
```

Install: `./install.sh`

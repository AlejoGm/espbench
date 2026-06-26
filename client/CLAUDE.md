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

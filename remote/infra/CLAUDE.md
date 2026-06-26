# remote/infra/

System-level infrastructure for Raspberry Pi. Handles device auto-discovery, session management, and service lifecycle.

## Files

| File | Type | Purpose |
|------|------|---------|
| `99-esp32.rules` | udev | Auto-create tmux session on USB plug-in |
| `esp32_tmux.sh` | Bash | Helper called by udev: creates tmux session, runs `remote_esp32.py` |
| `devremote` | Bash | CLI for session management (`devremote`, `--status`, `--reset`, `<N>`, `--unlock <N>`) |
| `devremote.service` | systemd | One-shot service: starts missing sessions at boot |
| `dashboard.service` | systemd | Runs FastAPI dashboard server |

## udev Rule (`99-esp32.rules`)

Triggers `esp32_tmux.sh` on every `ttyUSB*` add event:
```
ACTION=="add", SUBSYSTEM=="tty", KERNEL=="ttyUSB*", \
RUN+="/usr/bin/tmux new-session -d -s esp32_%k '/usr/local/bin/esp32_tmux.sh /dev/%k'"
```

## esp32_tmux.sh

- Arg: `/dev/ttyUSBN`
- Creates tmux session `esp32_ttyUSBN`
- Runs `remote_esp32.py --port-tty /dev/ttyUSBN --control-port 500N ...`
- TCP port = `5000 + N`

## devremote CLI

| Usage | Behavior |
|-------|----------|
| `devremote` | Scan devices, start missing sessions |
| `devremote --reset` | Kill and restart all sessions |
| `devremote --status` | Table: device / port / status / PID |
| `devremote <N>` | Attach to session for ttyUSBN |
| `devremote --unlock <N>` | Interactive unlock for ttyUSBN |

## Install Locations

```
/usr/local/bin/devremote
/usr/local/bin/esp32_tmux.sh
/etc/udev/rules.d/99-esp32.rules
/etc/systemd/system/devremote.service
/etc/systemd/system/dashboard.service
```

Installed by `../install.sh`.

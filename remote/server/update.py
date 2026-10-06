"""
update.py — lado Python de espbench-update (remote/infra/espbench-update).

- `busy_reason()`: por qué no conviene actualizar ahora (una placa flasheando o
  borrando, o con una reserva vigente). El script lo usa con
  `python -m server.update busy` (imprime el motivo, o nada).
- `read_status()` / `read_pin()` / `start_command()`: para GET/POST /api/update.
"""
import json
import re
import sys
from typing import List, Optional

from server import locks, paths, runstate

BUSY_STATES = ("flashing", "erasing")
# Rama, tag o commit: lo que git acepta y nada que parezca una opción.
REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
UPDATE_BIN = "/usr/local/bin/espbench-update"


def busy_reason() -> Optional[str]:
    for tty, st in runstate.list_all().items():
        if st.get("state") in BUSY_STATES and runstate.pid_alive(st.get("pid")):
            return f"{tty} {st['state']}"
    for f in sorted(paths.locks_dir().glob("*")) if paths.locks_dir().is_dir() else []:
        if f.suffix:            # locks/<tty>.lck es el flock, no un lock
            continue
        lock = locks.read(f.name)
        if lock is not None and lock.reservation:
            return f"{f.name} reservada por {lock.user}"
    return None


def read_status() -> Optional[dict]:
    try:
        return json.loads(paths.update_status_file().read_text())
    except (OSError, ValueError):
        return None


def read_pin() -> Optional[str]:
    try:
        lines = paths.update_conf_file().read_text().splitlines()
    except OSError:
        return None
    pins = [l[len("PIN="):].strip() for l in lines if l.startswith("PIN=")]
    return (pins[-1] if pins else "") or None


def valid_ref(ref: str) -> bool:
    return bool(REF_RE.fullmatch(ref)) and ".." not in ref


def start_command(ref: Optional[str], force: bool = False, unit: str = "espbench-update-manual") -> List[str]:
    """espbench-update en su propio unit de systemd: reinicia el dashboard, así que
    no puede ser un hijo de este proceso (moriría con él)."""
    cmd = ["sudo", "-n", "systemd-run", f"--unit={unit}", "--no-block", "--collect", UPDATE_BIN]
    cmd += ["--ref", ref] if ref else ["--release"]
    if force:
        cmd.append("--force")
    return cmd


if __name__ == "__main__":
    if sys.argv[1:] == ["busy"]:
        reason = busy_reason()
        if reason:
            print(reason)
    else:
        sys.exit("uso: python -m server.update busy")

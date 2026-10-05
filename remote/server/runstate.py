#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runstate.py — estado runtime de cada proceso remote_esp32.py, en run/<tty>.json.

Lo escribe el proceso que atiende el tty (Device, en cada transición de la
FSM) y lo leen otros procesos: el dashboard (DeviceRegistry, LogStreamer) y
devremote/esp32_tmux.sh. Keyed por tty y no por MAC porque es el estado del
*proceso*, y porque antes de leer la MAC no hay otra clave posible.

La escritura es atómica (archivo temporal + os.replace): un lector nunca ve
un JSON a medio escribir. Ver el bug de corrupción de devices.json, que es
exactamente lo que pasa sin esto.
"""
import json
import os
import pathlib
import tempfile
from typing import Optional

from server import paths


def write(tty_name: str, snapshot: dict) -> None:
    path = paths.tty_state_file(tty_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{tty_name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(snapshot, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read(tty_name: str) -> Optional[dict]:
    try:
        return json.loads(paths.tty_state_file(tty_name).read_text())
    except (OSError, ValueError):
        return None


def list_all() -> dict:
    """tty_name -> snapshot, de todos los run/*.json legibles."""
    out = {}
    d = paths.run_dir()
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            out[f.stem] = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
    return out


def pid_alive(pid) -> bool:
    """El proceso corre como root (sudo) y el dashboard como sfypi:
    os.kill(pid, 0) da PermissionError si existe pero es de otro usuario."""
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def remove(tty_name: str) -> None:
    try:
        paths.tty_state_file(tty_name).unlink()
    except OSError:
        pass

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
locks.py — lock de uso de un device: archivo locks/<tty>.

Formato: "user:token[:expires[:mac]]".
- "user:token" (sin vencimiento): el lock que toma el flash. Permanente hasta
  un unlock con el mismo par o hasta que esp32_tmux.sh relanza la sesión.
- con `expires` (epoch, segundos): una reserva (POST /api/device/{tty}/reserve).
  `mac` (12 hex, sin separadores: los ':' son el separador del archivo) es la
  placa reservada: si al arrancar el proceso del tty hay otra placa, la reserva
  se borra (los ttyUSB se renumeraron).

Vencido = inexistente en todos lados (LockStore del flash, DeviceRegistry, api):
read() lo ignora, y la próxima escritura lo pisa. No se borra al leerlo: entre
la lectura y el borrado otro proceso podía escribir una reserva nueva, y se
perdía. Ni user ni token pueden tener ':' (el formato no tendría cómo
separarlos).

Lo usan los dos procesos: el del device (protocol.LockStore, remote_esp32 al
arrancar) y el del api. Todo leer-decidir-escribir va dentro de exclusive(tty):
flock sobre locks/<tty>.lck (aparte, porque el lock en sí se reemplaza con
os.replace). La escritura es atómica (temp + os.replace) y los archivos quedan
666: los escribe root (device) y sfypi (api); locks/ es 777 (install.sh).
"""
import contextlib
import dataclasses
import datetime as dt
import fcntl
import os
import re
import tempfile
import time
from typing import Optional

from server import paths

_EXT_RE = re.compile(r"(?P<token>[^:]*):(?P<expires>\d+)(?::(?P<mac>[0-9A-Fa-f]{12}))?")


@dataclasses.dataclass
class Lock:
    user: str
    token: str
    expires: Optional[int] = None     # epoch; None = permanente (flash)
    mac: Optional[str] = None         # normalizada, 12 hex en mayúsculas

    @property
    def reservation(self) -> bool:
        return self.expires is not None

    def expired(self, now: Optional[float] = None) -> bool:
        return self.expires is not None and self.expires <= (time.time() if now is None else now)

    def owned_by(self, user: str, token: str) -> bool:
        return self.user == user and self.token == token

    def expires_iso(self) -> Optional[str]:
        """Hora local de la Pi, sin zona: para mensajes."""
        if self.expires is None:
            return None
        return dt.datetime.fromtimestamp(self.expires).isoformat(timespec="seconds")

    def expires_iso_tz(self) -> Optional[str]:
        """Con el offset de la Pi (2026-10-06T16:30:00-03:00): para datos que
        lee otro reloj (el navegador del dashboard puede estar en otra zona)."""
        if self.expires is None:
            return None
        return dt.datetime.fromtimestamp(self.expires).astimezone().isoformat(timespec="seconds")


def normalize_mac(mac: Optional[str]) -> Optional[str]:
    if not mac:
        return None
    return mac.upper().replace(":", "").replace("-", "")


def valid_credential(value: str) -> bool:
    return bool(value) and ":" not in value and "\n" not in value


def parse(text: str) -> Optional[Lock]:
    text = text.strip()
    if not text:
        return None
    user, _, rest = text.partition(":")
    m = _EXT_RE.fullmatch(rest)
    if m:
        return Lock(user, m.group("token"), int(m.group("expires")), normalize_mac(m.group("mac")))
    return Lock(user, rest)


def format_lock(lock: Lock) -> str:
    out = f"{lock.user}:{lock.token}"
    if lock.expires is not None:
        out += f":{int(lock.expires)}"
        if lock.mac:
            out += f":{normalize_mac(lock.mac)}"
    return out


def read(tty_name: str, now: Optional[float] = None) -> Optional[Lock]:
    """Lock vigente del tty, o None (no hay, o venció: se ignora)."""
    path = paths.lock_file(tty_name)
    try:
        lock = parse(path.read_text())
    except OSError:
        return None
    if lock is not None and lock.expired(now):
        return None
    return lock


@contextlib.contextmanager
def exclusive(tty_name: str):
    """Exclusión entre procesos para leer-decidir-escribir el lock de un tty."""
    path = paths.lock_file(tty_name).with_name(f"{tty_name}.lck")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o666)
    try:
        try:
            os.fchmod(fd, 0o666)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)        # suelta el flock


def write(tty_name: str, lock: Lock) -> None:
    path = paths.lock_file(tty_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{tty_name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(format_lock(lock))
        os.chmod(tmp, 0o666)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def remove(tty_name: str) -> None:
    try:
        paths.lock_file(tty_name).unlink()
    except OSError:
        pass


def drop_if_other_board(tty_name: str, mac: Optional[str]) -> Optional[Lock]:
    """Al arrancar el proceso del tty: una reserva de otra placa (los ttyUSB se
    renumeraron) no vale para esta. Devuelve la reserva borrada, o None."""
    with exclusive(tty_name):
        lock = read(tty_name)
        if lock is None or not lock.reservation or not lock.mac or not mac:
            return None
        if lock.mac == normalize_mac(mac):
            return None
        remove(tty_name)
        return lock

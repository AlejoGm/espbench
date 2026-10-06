#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
events.py — registro de eventos por placa (events.jsonl) y cursores del log.

Un evento es una línea JSON en devices/<MAC>/events.jsonl (o en
devices/unknown-<tty>/ hasta conocer la MAC):

    {"ts":"2026-10-05T16:02:03.123","type":"panic","cursor":"c:20261005_155000_812:48213",
     "detail":{"kind":"guru","reason":"LoadProhibited","line":"..."},"by":"device"}

El cursor apunta al log de la placa: c:<session_id>:<offset>, offset en bytes
siempre en fin de línea (= inicio de la línea del evento). Ver
docs/specs/agents-cli.md §3.4 y §4.

Escriben dos procesos (el del device y, desde la fase 2, el del api): append
con O_APPEND y un solo os.write() por línea (< 4 KB), atómico en Linux para
archivos locales. Sin flock, sin ids, sin rotación.

Este módulo no depende de DeviceLog: lo usan los dos procesos. El del device
escribe a través de DeviceLog.event() (conoce el cursor exacto); el del api,
con record(log_path, ...), que toma como cursor el fin de la última línea
completa del log en ese momento.
"""
import datetime as dt
import json
import os
import pathlib
import re
from typing import Optional

from server import paths

TYPES = ("session", "boot", "fw", "panic", "boot_loop", "state", "flash", "send", "reserve", "release")
MAX_EVENT_BYTES = 4000      # una línea por write(): por debajo de PIPE_BUF/página
_MAX_STR = 1000             # strings de detail más largos se recortan si la línea no entra

_CURSOR_RE = re.compile(r"c:(\d{8}_\d{6}_\d+):(\d+)")
_SESSION_RE = re.compile(r"sesi[oó]n (\d{8}_\d{6}_\d+)")


# ---------- cursores ----------

def format_cursor(session_id: str, offset: int) -> str:
    return f"c:{session_id}:{offset}"


def parse_cursor(cursor: Optional[str]):
    """c:<sesión>:<offset> → (sesión, offset), o None si no es un cursor."""
    m = _CURSOR_RE.fullmatch(cursor or "")
    return (m.group(1), int(m.group(2))) if m else None


def read_session_id(log_path) -> Optional[str]:
    """session_id del header de un output.log, o None (log viejo o vacío)."""
    try:
        with open(log_path, "rb") as f:
            first = f.readline(1024).decode("utf-8", errors="replace")
    except OSError:
        return None
    m = _SESSION_RE.search(first)
    return m.group(1) if m else None


def log_end_cursor(log_path) -> Optional[str]:
    """Cursor del fin de la última línea completa del log, o None si el log no
    tiene header de sesión. Lo usa el proceso que no escribe el log (api)."""
    session = read_session_id(log_path)
    if session is None:
        return None
    try:
        with open(log_path, "rb") as f:
            end = f.seek(0, os.SEEK_END)
            pos = end
            while pos > 0:
                start = max(0, pos - 8192)
                f.seek(start)
                chunk = f.read(pos - start)
                i = chunk.rfind(b"\n")
                if i >= 0:
                    return format_cursor(session, start + i + 1)
                pos = start
    except OSError:
        return None
    return format_cursor(session, 0)


# ---------- eventos ----------

def iso_ms(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch).isoformat(timespec="milliseconds")


def make(type_: str, cursor: Optional[str], detail: Optional[dict] = None,
         by: str = "device", ts: Optional[float] = None) -> dict:
    return {"ts": iso_ms(ts if ts is not None else dt.datetime.now().timestamp()),
            "type": type_, "cursor": cursor, "detail": detail or {}, "by": by}


def encode(event: dict) -> bytes:
    data = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) <= MAX_EVENT_BYTES:
        return data
    detail = {k: (v[:_MAX_STR] if isinstance(v, str) else v) for k, v in (event.get("detail") or {}).items()}
    data = (json.dumps({**event, "detail": detail}, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(data) <= MAX_EVENT_BYTES:
        return data
    return (json.dumps({**event, "detail": {"truncated": True}}, separators=(",", ":")) + "\n").encode()


def append(path: pathlib.Path, event: dict) -> None:
    """Agrega una línea con un solo write() en O_APPEND (atómico entre procesos)."""
    _append_raw(pathlib.Path(path), encode(event))


def _append_raw(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
    try:
        try:
            os.fchmod(fd, 0o666)     # lo crea el proceso del device (root); el api también escribe
        except OSError:
            pass
        os.write(fd, data)
    finally:
        os.close(fd)


def read(path) -> list:
    """Todos los eventos del archivo, en orden. Las líneas inválidas se saltean."""
    out = []
    try:
        raw = pathlib.Path(path).read_bytes()
    except OSError:
        return out
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def record(log_path, type_: str, detail: Optional[dict] = None, by: str = "api") -> Optional[dict]:
    """Evento desde un proceso que no escribe el log (api: send, reserve,
    release). Va al events.jsonl de al lado del log, con el cursor del fin de la
    última línea completa. None si el log no tiene sesión (no hay a qué apuntar)."""
    cursor = log_end_cursor(log_path)
    if cursor is None:
        return None
    ev = make(type_, cursor, detail, by=by)
    append(paths.events_file_beside(log_path), ev)
    return ev


def migrate_session(src: pathlib.Path, dst: pathlib.Path, session_id: str) -> int:
    """unknown-<tty> → MAC: mueve solo los eventos de esa sesión (el provisorio
    puede tener sesiones de otra placa en el mismo tty). Devuelve cuántos movió."""
    try:
        lines = src.read_bytes().splitlines(keepends=True)
    except OSError:
        return 0
    mine, others = [], []
    for line in lines:
        try:
            cur = parse_cursor(json.loads(line).get("cursor"))
        except (ValueError, AttributeError):
            cur = None
        (mine if cur and cur[0] == session_id else others).append(line)
    for line in mine:
        _append_raw(dst, line)
    if others:
        tmp = src.with_name(src.name + ".tmp")
        tmp.write_bytes(b"".join(others))
        try:
            os.chmod(tmp, 0o666)     # el api (sfypi) tiene que poder seguir escribiendo
        except OSError:
            pass
        os.replace(tmp, src)
    else:
        try:
            src.unlink()
        except OSError:
            pass
    return len(mine)

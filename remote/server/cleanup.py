#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cleanup.py — liberar espacio en el bench: sesiones de log rotadas y flasheos viejos.

Qué borra (lo que no se modificó en los últimos `days` días):
- devices/<MAC>/output_<sesión>.log (y en devices/unknown-<tty>/): sesiones de log rotadas por DeviceLog.
  Nunca output.log: es la sesión en curso.
- devices/<MAC>/jobs/job_*/ y jobs/job_*_<tty>/ (esquema anterior): artifact.zip, bins, firmware.elf, job.log,
  result.json. De cada placa queda siempre el flasheo más reciente (el "último flash" del dashboard).

No toca events.jsonl (los eventos de una sesión borrada quedan; su cursor da `cursor_expired`), current.elf,
devices.json, meta/, locks/ ni run/.

La vista previa (plan) solo lee, la hace el api (sfypi). Borrar necesita root (devices/<MAC>/ lo crea el
proceso del device): el api lanza `sudo -n <python> -m server.cleanup --days N --json --base <ESP_BASE>`.
A mano en la Pi: `cd /opt/esp && sudo venv/bin/python -m server.cleanup --days 14 --dry-run`.
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import sys
import time
from typing import Optional

from server import paths

DEFAULT_DAYS = 14
MIN_DAYS = 1
MAX_DAYS = 3650

_SESSION_RE = re.compile(r"output_[0-9_]+\.log")
_JOB_RE = re.compile(r"job_[A-Za-z0-9_.-]+")


def _newest_mtime(p: pathlib.Path) -> float:
    """mtime de un archivo, o el más nuevo entre un directorio y lo que tiene adentro (un job en curso
    escribe job.log aunque el directorio no cambie)."""
    st = p.lstat()
    newest = st.st_mtime
    if p.is_dir() and not p.is_symlink():
        for root, dirs, files in os.walk(p):
            for name in dirs + files:
                try:
                    newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
                except OSError:
                    pass
    return newest


def _size(p: pathlib.Path) -> int:
    if p.is_symlink() or not p.is_dir():
        return p.lstat().st_size
    total = 0
    for root, _dirs, files in os.walk(p):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _job_dirs_by_owner() -> dict:
    """{dueño: [job dirs]}: dueño = devices/<MAC>/ o "legacy:<tty>" para jobs/job_*_<tty>."""
    owners = {}
    devices = paths.devices_dir()
    if devices.is_dir():
        for home in devices.iterdir():
            jobs = home / "jobs"
            if home.is_symlink() or jobs.is_symlink() or not jobs.is_dir():
                continue
            owners[str(home)] = [j for j in jobs.iterdir()
                                 if _JOB_RE.fullmatch(j.name) and j.is_dir() and not j.is_symlink()]
    legacy = paths.jobs_dir()
    if legacy.is_dir() and not legacy.is_symlink():
        for j in legacy.iterdir():
            if _JOB_RE.fullmatch(j.name) and j.is_dir() and not j.is_symlink():
                owners.setdefault("legacy:" + j.name.rsplit("_", 1)[-1], []).append(j)
    return owners


def plan(days: int = DEFAULT_DAYS, now: Optional[float] = None) -> list:
    """Lo que se borraría: [{path, kind: "session"|"job", bytes, mtime}], más viejo primero. Solo lee."""
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise ValueError(f"days entre {MIN_DAYS} y {MAX_DAYS}")
    cutoff = (now if now is not None else time.time()) - days * 86400
    items = []

    def add(p: pathlib.Path, kind: str, mtime: float) -> None:
        try:
            items.append({"path": str(p), "kind": kind, "bytes": _size(p), "mtime": mtime})
        except OSError:
            pass

    devices = paths.devices_dir()
    if devices.is_dir():
        for home in devices.iterdir():
            if home.is_symlink() or not home.is_dir():
                continue
            for f in home.iterdir():
                if not _SESSION_RE.fullmatch(f.name) or f.is_symlink() or not f.is_file():
                    continue
                try:
                    mtime = f.lstat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    add(f, "session", mtime)

    for jobs in _job_dirs_by_owner().values():
        dated = []
        for j in jobs:
            try:
                dated.append((_newest_mtime(j), j))
            except OSError:
                pass
        dated.sort(key=lambda t: (t[0], t[1].name))
        for mtime, j in dated[:-1]:      # el más reciente de cada placa queda siempre
            if mtime < cutoff:
                add(j, "job", mtime)

    items.sort(key=lambda i: i["mtime"])
    return items


def summary(items: list, days: int) -> dict:
    out = {"days": days, "total_bytes": sum(i["bytes"] for i in items)}
    for kind, key in (("session", "sessions"), ("job", "jobs")):
        mine = [i for i in items if i["kind"] == kind]
        out[key] = {"count": len(mine), "bytes": sum(i["bytes"] for i in mine)}
    return out


def _inside_base(p: pathlib.Path) -> bool:
    base = paths.esp_base().resolve()
    try:
        p.resolve().relative_to(base)
    except ValueError:
        return False
    return p.resolve() != base


def apply(items: list) -> tuple:
    """Borra lo de plan() (solo dentro de ESP_BASE). Devuelve (borrados, errores)."""
    done, errors = [], []
    for i in items:
        p = pathlib.Path(i["path"])
        if not _inside_base(p):
            errors.append({"path": i["path"], "error": "fuera de ESP_BASE"})
            continue
        try:
            if i["kind"] == "job":
                shutil.rmtree(p)
            else:
                p.unlink()
            done.append(i)
        except FileNotFoundError:
            pass
        except OSError as e:
            errors.append({"path": i["path"], "error": str(e)})
    return done, errors


def run(days: int = DEFAULT_DAYS, now: Optional[float] = None) -> dict:
    """plan() + apply(): summary() de lo borrado con `freed_bytes` y `errors`. Sin taglog: su sink es stdout, que
    con --json lee el api."""
    items = plan(days, now)
    done, errors = apply(items)
    res = summary(done, days)
    res["freed_bytes"] = res.pop("total_bytes")
    res["errors"] = errors
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m server.cleanup",
                                 description="Borra sesiones de log rotadas y flasheos viejos del bench.")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"más viejos que N días (default {DEFAULT_DAYS})")
    ap.add_argument("--dry-run", action="store_true", help="solo mostrar qué se borraría")
    ap.add_argument("--json", action="store_true", help="salida JSON (la usa el api)")
    ap.add_argument("--base", help="ESP_BASE (sudo no pasa el entorno)")
    args = ap.parse_args(argv)
    if args.base:
        os.environ["ESP_BASE"] = args.base
    try:
        if args.dry_run:
            items = plan(args.days)
            res = summary(items, args.days)
        else:
            items = None
            res = run(args.days)
    except ValueError as e:
        print(json.dumps({"error": str(e)}) if args.json else f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(res))
        return 0
    if items is not None:
        for i in items:
            print(f"{i['kind']:7} {i['bytes'] / 1e6:8.1f} MB  {i['path']}")
    mb = (res.get("freed_bytes", res.get("total_bytes")) or 0) / 1e6
    verb = "se liberarían" if args.dry_run else "liberados"
    print(f"{res['sessions']['count']} sesiones, {res['jobs']['count']} jobs: {mb:.1f} MB {verb}")
    for e in res.get("errors", []):
        print(f"error: {e['path']}: {e['error']}", file=sys.stderr)
    return 1 if res.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())

"""
history.py — historial de un device para el dashboard: flasheos (jobs) y
sesiones de log anteriores.

Solo lectura de disco. Los jobs viven en devices/<mac>/jobs/<job_id>/ (y, del
esquema anterior o sin MAC, en jobs/<job_id>_<tty>/); cada uno tiene job.log y,
desde que protocol.py lo escribe, result.json. Las sesiones son los
output_<ts>.log que deja DeviceLog al rotar, al lado del output.log actual.

Todo nombre que llega de la URL se valida contra un patrón antes de armar una
ruta: nada de `..` ni separadores.
"""
import json
import pathlib
import re
from typing import Optional

from server import paths, runstate

JOB_ID_RE = re.compile(r"job_[A-Za-z0-9_.-]+")
SESSION_RE = re.compile(r"output(_[0-9_]+)?\.log")
_JOB_TS_RE = re.compile(r"job_(\d{8})_(\d{6})")

MAX_READ = 2 * 1024 * 1024   # lo que se devuelve de un log como mucho (la cola)


def _job_dirs(tty_name: str, mac: Optional[str]) -> list:
    dirs = []
    if mac:
        d = paths.device_jobs_dir(mac)
        if d.is_dir():
            dirs += [p for p in d.iterdir() if p.is_dir() and JOB_ID_RE.fullmatch(p.name)]
    legacy = paths.jobs_dir()
    if legacy.is_dir():
        dirs += [p for p in legacy.glob(f"job_*_{tty_name}") if p.is_dir()]
    return dirs


def job_timestamp(name: str) -> Optional[str]:
    m = _JOB_TS_RE.match(name)
    if not m:
        return None
    d, t = m.groups()
    return f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:]}"


def _read_result(jobdir: pathlib.Path) -> Optional[dict]:
    try:
        return json.loads((jobdir / "result.json").read_text())
    except (OSError, ValueError):
        return None


def list_jobs(tty_name: str, mac: Optional[str], limit: int = 30) -> list:
    """Flasheos del device, el más reciente primero. `ok` es None en jobs
    anteriores a result.json (no se sabe cómo terminaron)."""
    jobs = []
    for d in _job_dirs(tty_name, mac):
        r = _read_result(d) or {}
        jobs.append({
            "job_id": d.name,
            "ts": job_timestamp(d.name) or r.get("requested_at"),
            "finished_at": r.get("finished_at"),
            "ok": r.get("ok"),
            "status": r.get("status"),
            "user": r.get("user"),
            "error": r.get("error"),
            "message": r.get("message") or r.get("error_hint"),
            "missing_app": r.get("missing_app"),
            "has_log": (d / "job.log").exists(),
        })
    jobs.sort(key=lambda j: j["ts"] or "", reverse=True)
    return jobs[:limit]


def job_log(tty_name: str, mac: Optional[str], job_id: str) -> Optional[str]:
    if not JOB_ID_RE.fullmatch(job_id):
        return None
    for d in _job_dirs(tty_name, mac):
        if d.name == job_id:
            return _tail(d / "job.log")
    return None


def _log_dir(tty_name: str, mac: Optional[str]) -> Optional[pathlib.Path]:
    """Donde están output.log y las sesiones rotadas: lo dice el estado runtime
    (cubre el hogar provisorio de un device sin MAC); si no, devices/<mac>/."""
    state = runstate.read(tty_name) or {}
    if state.get("log_path"):
        return pathlib.Path(state["log_path"]).parent
    if mac:
        return paths.device_home(mac)
    return None


def list_sessions(tty_name: str, mac: Optional[str]) -> list:
    """Sesiones de log: la actual (output.log) y las rotadas, la más reciente primero."""
    d = _log_dir(tty_name, mac)
    if d is None or not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        if not SESSION_RE.fullmatch(p.name):
            continue
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime,
                    "current": p.name == "output.log"})
    out.sort(key=lambda s: (s["current"], s["mtime"]), reverse=True)
    return out


def session_path(tty_name: str, mac: Optional[str], name: str) -> Optional[pathlib.Path]:
    if not SESSION_RE.fullmatch(name):
        return None
    d = _log_dir(tty_name, mac)
    if d is None:
        return None
    p = d / name
    return p if p.is_file() else None


def _tail(path: pathlib.Path, limit: Optional[int] = None) -> Optional[str]:
    limit = limit or MAX_READ
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def read_session(tty_name: str, mac: Optional[str], name: str) -> Optional[str]:
    p = session_path(tty_name, mac, name)
    return _tail(p) if p else None

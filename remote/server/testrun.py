"""
testrun.py — la corrida de tests (`espbench test`) de una placa, para el dashboard.

El cliente corre los tests (client/unity_runner.py) y le avisa al api cada paso: `POST /api/device/{tty}/test` con
phase start | case | result | end. Cada aviso queda como evento `test` en events.jsonl (historial, marcas del log) y
actualiza meta/test_runs/<MAC>.json (paths.device_test_run): la última corrida (lo que muestra la card: progreso, resultado) y las ventanas
de las corridas recientes.

Dentro de una ventana, los reinicios y panics son de los tests (un multietapa reinicia a propósito, un test puede
esperar un panic): no ponen la placa en rojo ni cuentan como problemas (device_registry.acked,
benchinfo.device_activity). Un test que falla se ve en el resultado de la corrida.

Una corrida sin avisos por más de `stale_s` (el cliente se cortó) queda `interrupted`; su ventana termina en el último
aviso. Solo lo escribe el api (un proceso): escritura atómica, sin flock.
"""
import contextlib
import json
import os
import re
import tempfile
from typing import List, Optional, Tuple

from server import paths

PHASES = ("start", "case", "result", "end")
RESULTS = ("pass", "fail", "timeout", "panic", "ignored", "skipped")
FAILED = ("fail", "timeout", "panic")
END_STATUS = ("done", "aborted")
DEFAULT_STALE_S = 600
MAX_CASES = 1000
MAX_WINDOWS = 50
WINDOW_KEEP_S = 48 * 3600
# Un boot loop se cierra BOOT_LOOP_WINDOW (60 s) después del último boot: el que empezó en la corrida sigue
# activo un rato después de que termina.
LOOP_GRACE_S = 90
SLACK_S = 1.0
_RUN_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_MSG_MAX = 300


class TestRunError(ValueError):
    """Aviso que no corresponde (sin corrida, otra corrida, campo inválido). `error` es el del contrato."""

    def __init__(self, error: str, message: str):
        super().__init__(message)
        self.error = error
        self.message = message


def read(mac: str) -> Optional[dict]:
    try:
        d = json.loads(paths.device_test_run(mac).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _write(mac: str, data: dict) -> None:
    path = paths.device_test_run(mac)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".test_run.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.chmod(tmp, 0o666)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _status(run: dict, now: float) -> str:
    if run.get("status") == "running" and now - (run.get("updated") or 0) > (run.get("stale_s") or DEFAULT_STALE_S):
        return "interrupted"
    return run.get("status") or "running"


def _window(run: dict, now: float) -> Optional[Tuple[float, float]]:
    start = run.get("started")
    if start is None:
        return None
    status = _status(run, now)
    end = run.get("ended") if status in END_STATUS else (now if status == "running" else run.get("updated"))
    return (float(start), float(end if end is not None else now))


def view(mac: str, now: float) -> Optional[dict]:
    """La última corrida para /api/devices (sin la lista de casos): status running | done | aborted | interrupted."""
    run = read(mac)
    if not run or not run.get("run_id"):
        return None
    out = {k: run.get(k) for k in ("run_id", "user", "project", "total", "done", "passed", "failed", "counts",
                                   "current", "started", "updated", "ended")}
    out["status"] = _status(run, now)
    if out["status"] != "running":
        out["current"] = None
    return out


def detail(mac: str, now: float) -> Optional[dict]:
    """La última corrida completa, con sus casos (pestaña Tests del monitor)."""
    run = read(mac)
    if not run or not run.get("run_id"):
        return None
    out = {k: v for k, v in run.items() if k != "windows"}
    out["status"] = _status(run, now)
    if out["status"] != "running":
        out["current"] = None
    return out


def windows(mac: str, now: float) -> List[Tuple[float, float]]:
    """Ventanas [inicio, fin] de las corridas recientes, la actual incluida (fin = ahora si sigue corriendo)."""
    run = read(mac)
    if not run:
        return []
    out = [tuple(w) for w in run.get("windows") or [] if isinstance(w, list) and len(w) == 2]
    cur = _window(run, now)
    if cur:
        out.append(cur)
    return out


def in_windows(t: Optional[float], wins: List[Tuple[float, float]]) -> bool:
    """`health` trae las horas al segundo (SerialWatch): un margen de un segundo en cada punta."""
    return t is not None and any(a - SLACK_S <= t <= b + SLACK_S for a, b in wins)


def loop_covered(wins: List[Tuple[float, float]], now: float) -> bool:
    """Un boot loop activo ahora es de los tests si hay una corrida en curso o que terminó hace muy poco."""
    return any(b + LOOP_GRACE_S >= now for _, b in wins)


# ---------- avisos del cliente ----------

def _str(v, n: int = 200) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s[:n] or None


def _int(v, name: str, lo: int, hi: int, default=None) -> Optional[int]:
    if v is None:
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise TestRunError("bad_request", f"{name} tiene que ser un entero")
    if not lo <= n <= hi:
        raise TestRunError("bad_request", f"{name} entre {lo} y {hi}")
    return n


def _case(body: dict, with_result: bool) -> dict:
    c = body.get("case")
    if not isinstance(c, dict):
        raise TestRunError("bad_request", "falta case")
    out = {"index": _int(c.get("index"), "case.index", 1, MAX_CASES),
           "number": _int(c.get("number"), "case.number", 0, 100000),
           "name": _str(c.get("name")) or "?"}
    if out["index"] is None:
        raise TestRunError("bad_request", "falta case.index")
    if with_result:
        res = c.get("result")
        if res not in RESULTS:
            raise TestRunError("bad_request", f"case.result tiene que ser uno de {', '.join(RESULTS)}")
        out["result"] = res
        try:
            out["duration_s"] = round(float(c.get("duration_s") or 0), 3)
        except (TypeError, ValueError):
            out["duration_s"] = 0.0
        for k in ("message", "file"):
            v = _str(c.get(k), _MSG_MAX)
            if v:
                out[k] = v
        for k in ("line", "stage"):
            if c.get(k) is not None:
                out[k] = _int(c.get(k), f"case.{k}", 0, 10 ** 7)
    return out


def apply(mac: str, body: dict, now: float, cursor: Optional[str] = None) -> dict:
    """Aplica un aviso y devuelve el detail del evento `test`. cursor: el del log al recibirlo (para el caso)."""
    phase = body.get("phase")
    if phase not in PHASES:
        raise TestRunError("bad_request", f"phase tiene que ser uno de {', '.join(PHASES)}")
    run_id = str(body.get("run_id") or "")
    if not _RUN_ID_RE.fullmatch(run_id):
        raise TestRunError("bad_request", "run_id inválido")
    run = read(mac) or {}

    if phase == "start":
        wins = [w for w in run.get("windows") or [] if isinstance(w, list) and len(w) == 2]
        prev = _window(run, now) if run.get("run_id") and run.get("run_id") != run_id else None
        if prev:
            wins.append(list(prev))
        wins = [w for w in wins if w[1] >= now - WINDOW_KEEP_S][-MAX_WINDOWS:]
        run = {"run_id": run_id, "status": "running", "user": _str(body.get("user"), 64),
               "project": _str(body.get("project"), 100),
               "total": _int(body.get("total"), "total", 0, MAX_CASES, 0),
               "stale_s": _int(body.get("stale_s"), "stale_s", 30, 24 * 3600, DEFAULT_STALE_S),
               "done": 0, "passed": 0, "failed": 0, "counts": {}, "current": None, "cases": [],
               "started": now, "updated": now, "ended": None, "cursor": cursor, "windows": wins}
        _write(mac, run)
        return {"phase": phase, "run_id": run_id, "total": run["total"], "project": run["project"],
                "user": run["user"]}

    if run.get("run_id") != run_id:
        raise TestRunError("not_found", f"no hay una corrida {run_id} en esta placa (falta el start)")
    if phase != "end" and run.get("status") != "running":
        raise TestRunError("bad_request", f"la corrida {run_id} ya terminó")
    run["updated"] = now
    if body.get("stale_s") is not None:
        run["stale_s"] = _int(body.get("stale_s"), "stale_s", 30, 24 * 3600)
    out = {"phase": phase, "run_id": run_id}

    if phase == "case":
        c = _case(body, with_result=False)
        run["current"] = {**c, "started": now, "cursor": cursor}
        out["case"] = c
    elif phase == "result":
        c = _case(body, with_result=True)
        cur = run.get("current") or {}
        c["cursor"] = cur.get("cursor") if cur.get("index") == c["index"] else None
        c["end_cursor"] = cursor
        run["cases"] = (run.get("cases") or [])[-(MAX_CASES - 1):] + [c]
        counts = run.get("counts") or {}
        counts[c["result"]] = counts.get(c["result"], 0) + 1
        run["counts"] = counts
        run["done"] = max(run.get("done") or 0, c["index"])
        run["passed"] = counts.get("pass", 0)
        run["failed"] = sum(counts.get(k, 0) for k in FAILED)
        run["current"] = None
        out["case"] = {k: v for k, v in c.items() if k not in ("cursor", "end_cursor")}
    else:
        status = body.get("status") or "done"
        if status not in END_STATUS:
            raise TestRunError("bad_request", f"status tiene que ser uno de {', '.join(END_STATUS)}")
        if run.get("status") != "running":
            return {**out, "status": run.get("status"), "repeated": True}
        run.update({"status": status, "ended": now, "current": None, "end_cursor": cursor})
        out.update({"status": status, "total": run.get("total"), "done": run.get("done"),
                    "passed": run.get("passed"), "failed": run.get("failed"), "counts": run.get("counts"),
                    "duration_s": round(now - run["started"], 1), "project": run.get("project"),
                    "user": run.get("user")})
        msg = _str(body.get("message"), _MSG_MAX)
        if msg:
            out["message"] = run["message"] = msg
    _write(mac, run)
    return out

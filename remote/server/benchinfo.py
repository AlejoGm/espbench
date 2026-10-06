"""
benchinfo.py — lo que el dashboard muestra del bench entero (no de una placa):

- `health()`: la máquina (temperatura, RAM, disco, carga, uptime, modelo). Lee
  /proc y /sys; en una máquina sin esos archivos (Mac, tests) cada dato que falta
  queda en None.
- `activity()`: cuántos reinicios, panics, flashes, boot loops y reservas tuvo
  cada placa por hora en las últimas N horas, y los eventos recientes que
  merecen un aviso (panic, boot loop, flash). Sale de devices/<mac>/events.jsonl,
  leído de atrás para adelante hasta la ventana.
"""
import datetime as dt
import os
import pathlib
import shutil
import functools
import socket
import time
import uuid
from typing import Dict, List, Optional

from server import events, paths

# Tipos que se cuentan por hora. reserve cuenta la hora en la que se tomó la reserva.
BUCKET_TYPES = ("boot", "panic", "flash", "boot_loop", "reserve")
# Eventos que van a la lista de recientes (los avisos de bench-master).
NOTABLE = ("panic", "boot_loop", "flash")


# ---------- salud de la máquina ----------

def _read(root: pathlib.Path, rel: str) -> Optional[str]:
    try:
        return (root / rel).read_text().strip("\x00\n ")
    except OSError:
        return None


def _meminfo(text: Optional[str]) -> Dict[str, int]:
    out = {}
    for line in (text or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0])            # kB
    return out


def health(root: str = "/", disk_path: Optional[str] = None) -> dict:
    r = pathlib.Path(root)
    temp = _read(r, "sys/class/thermal/thermal_zone0/temp")
    mem = _meminfo(_read(r, "proc/meminfo"))
    uptime = _read(r, "proc/uptime")
    loadavg = _read(r, "proc/loadavg")
    try:
        du = shutil.disk_usage(disk_path or str(paths.esp_base()))
        disk = {"total_gb": round(du.total / 1e9, 1), "used_pct": round(du.used / du.total * 100)}
    except OSError:
        disk = None
    ram = None
    if mem.get("MemTotal") and "MemAvailable" in mem:
        total = mem["MemTotal"]
        ram = {"total_mb": round(total / 1024), "used_pct": round((total - mem["MemAvailable"]) / total * 100)}
    load = None
    if loadavg:
        try:
            load = {"1m": float(loadavg.split()[0]), "cpus": os.cpu_count() or 1}
        except ValueError:
            pass
    return {
        "hostname": socket.gethostname(),
        "model": _read(r, "proc/device-tree/model"),
        "temp_c": round(int(temp) / 1000, 1) if temp and temp.lstrip("-").isdigit() else None,
        "ram": ram,
        "disk": disk,
        "load": load,
        "uptime_s": int(float(uptime.split()[0])) if uptime else None,
    }


# Interfaces que no son la placa de red de la máquina (la MAC cambia o no existe).
_VIRTUAL = ("lo", "tailscale", "docker", "veth", "br-", "virbr", "wg", "tun", "tap", "zt")


def host_id(root: str = "/") -> Optional[str]:
    """MAC de la máquina: la identidad del bench para bench-master, que no cambia
    si se renombra (bench_name), cambia de IP o se lo ve por LAN y por Tailscale.
    eth0/end0/wlan0 primero; si no hay /sys (Mac), la que da uuid.getnode()."""
    net = pathlib.Path(root) / "sys/class/net"
    try:
        names = sorted(p.name for p in net.iterdir())
    except OSError:
        names = []
    for n in [n for n in ("eth0", "end0", "wlan0") if n in names] + [n for n in names if not n.startswith(_VIRTUAL)]:
        mac = _read(pathlib.Path(root), f"sys/class/net/{n}/address")
        if mac and mac != "00:00:00:00:00:00":
            return mac.lower()
    if root != "/":
        return None
    node = uuid.getnode()
    if (node >> 40) & 1:            # bit multicast: uuid lo inventó, no es una MAC real
        return None
    return ":".join(f"{(node >> s) & 0xff:02x}" for s in range(40, -1, -8))


@functools.lru_cache(maxsize=1)
def this_host_id() -> Optional[str]:
    return host_id()


# ---------- actividad por hora ----------

def _epoch(ts: Optional[str]) -> Optional[float]:
    try:
        return dt.datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def device_activity(events_path, hours: int, now: float) -> dict:
    """{"buckets": [{boot, panic, ...} × hours], "recent": [eventos notables]}.
    El bucket 0 es la hora más vieja; el último termina en `now`."""
    start = now - hours * 3600
    buckets = [dict.fromkeys(BUCKET_TYPES, 0) for _ in range(hours)]
    recent = []
    for raw in events.read_back(events_path):
        ev = events.parse_line(raw)
        if ev is None:
            continue
        t = _epoch(ev.get("ts"))
        if t is None:
            continue
        if t < start:
            break                           # de atrás para adelante: lo que sigue es más viejo
        typ = ev.get("type")
        if typ == "boot_loop" and (ev.get("detail") or {}).get("phase") == "end":
            continue                        # cuenta el inicio, no el fin
        if typ in BUCKET_TYPES:
            i = min(hours - 1, int((t - start) // 3600))
            buckets[i][typ] += 1
        if typ in NOTABLE:
            recent.append({"ts": ev["ts"], "type": typ, "detail": ev.get("detail") or {}})
    return {"buckets": buckets, "recent": recent[:20]}


def activity(devices: List[dict], hours: int = 24, now: Optional[float] = None) -> dict:
    """Por placa (las de /api/devices con MAC): actividad de las últimas `hours` horas."""
    now = time.time() if now is None else now
    out = {}
    for d in devices:
        if not d.get("mac"):
            continue
        a = device_activity(paths.device_events_file(d["mac"]), hours, now)
        out[d["tty_name"]] = {"key": d.get("device_key") or d.get("sn") or d["mac"], "mac": d["mac"], **a}
    return {"now": events.iso_ms(now), "hours": hours, "devices": out}

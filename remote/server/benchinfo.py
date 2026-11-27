"""
benchinfo.py — lo que el dashboard muestra del bench entero (no de una placa):

- `health()`: la máquina (temperatura, RAM, disco, carga, uptime, modelo). Lee
  /proc y /sys; en una máquina sin esos archivos (Mac, tests) cada dato que falta
  queda en None. Sirve para una Pi y para una PC x86 (temperatura de la CPU y
  modelo del DMI: ver `_cpu_temp` y `_model`).
- `activity()`: cuántos reinicios, panics, flashes, boot loops y reservas tuvo
  cada placa por hora en las últimas N horas, y los eventos recientes que
  merecen un aviso (panic, boot loop, flash). Sale de devices/<mac>/events.jsonl,
  leído de atrás para adelante hasta la ventana. Con un ACK de la placa (ack_at),
  `totals` cuenta solo lo de después y `acked` dice cuántas horas quedaron antes.
  Los reinicios, panics y boot loops durante una corrida de `espbench test`
  (testrun.windows) no se cuentan ni avisan: van como `test_boot` / `test_panic`;
  `test` y `test_fail` cuentan los casos que terminaron en esa hora.
"""
import datetime as dt
import os
import pathlib
import shutil
import functools
import math
import socket
import time
import uuid
from typing import Dict, List, Optional

from server import events, paths, testrun

# Tipos que se cuentan por hora. reserve cuenta la hora en la que se tomó la reserva.
BUCKET_TYPES = ("boot", "panic", "flash", "boot_loop", "reserve")
# Durante una corrida de tests: casos terminados (y los que fallaron), y los reinicios/panics de los tests.
TEST_KEYS = ("test", "test_fail", "test_boot", "test_panic")
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


def _millideg(text: Optional[str]) -> Optional[int]:
    return int(text) if text and text.lstrip("-").isdigit() else None


# Dónde está la temperatura de la CPU, de la más confiable a la menos. En la Pi thermal_zone0
# es cpu-thermal. En una PC thermal_zone0 suele ser acpitz, una sonda del mother que en muchas
# da siempre lo mismo (27,8 °C): la CPU es x86_pkg_temp (Intel) o, por hwmon, coretemp (Intel)
# y k10temp / zenpower (AMD, que no tiene thermal zone de la CPU).
_CPU_ZONES = ("x86_pkg_temp", "cpu-thermal", "cpu_thermal")
_CPU_HWMON = ("coretemp", "k10temp", "zenpower")


def _cpu_temp(r: pathlib.Path) -> Optional[int]:
    """m°C de la CPU; si no se reconoce ninguna, thermal_zone0 (lo de siempre)."""
    zones = {}
    for z in sorted((r / "sys/class/thermal").glob("thermal_zone*")):
        zones.setdefault(_read(z, "type"), z)
    for t in _CPU_ZONES:
        v = _millideg(_read(zones[t], "temp")) if t in zones else None
        if v is not None:
            return v
    for h in sorted((r / "sys/class/hwmon").glob("hwmon*")):
        if _read(h, "name") in _CPU_HWMON:
            v = _millideg(_read(h, "temp1_input"))
            if v is not None:
                return v
    return _millideg(_read(r, "sys/class/thermal/thermal_zone0/temp"))


# Lo que los fabricantes dejan en el DMI cuando no completan el campo.
_DMI_PLACEHOLDERS = ("to be filled", "system product name", "default string", "not applicable",
                     "not specified", "o.e.m.", "type1productconfigid")


def _dmi(r: pathlib.Path, field: str) -> Optional[str]:
    v = _read(r, f"sys/class/dmi/id/{field}")
    if not v or any(p in v.lower() for p in _DMI_PLACEHOLDERS):
        return None
    return v


def _model(r: pathlib.Path) -> Optional[str]:
    """La Pi: device-tree ("Raspberry Pi 4 Model B Rev 1.4"). Una PC: el DMI. Un equipo de marca
    trae el modelo en product_name ("OptiPlex 7050"); un mother armado deja ahí un código que
    también está en board_name ("MS-7B29" en "H310M PRO-VDH (MS-7B29)"), y entonces va el mother."""
    dt_model = _read(r, "proc/device-tree/model")
    if dt_model:
        return dt_model
    product, board = _dmi(r, "product_name"), _dmi(r, "board_name")
    if product and not (board and product in board):
        return product
    return board


def health(root: str = "/", disk_path: Optional[str] = None) -> dict:
    r = pathlib.Path(root)
    temp = _cpu_temp(r)
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
        "model": _model(r),
        "temp_c": round(temp / 1000, 1) if temp is not None else None,
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


def device_activity(events_path, hours: int, now: float, ack: Optional[float] = None, test_windows=()) -> dict:
    """{"buckets": [{boot, panic, ...} × hours], "totals": {boot, panic, ...}, "acked": n,
    "recent": [eventos notables]}. El bucket 0 es la hora más vieja; el último termina en
    `now`. ack (epoch del ACK): totals cuenta solo lo posterior; acked = los primeros n
    buckets, los que empezaron antes del ACK (el del ACK, solo si no tuvo nada después).
    Sin ACK, totals = suma de los buckets. test_windows: lo de adentro es de los tests (ver arriba)."""
    start = now - hours * 3600
    wins = list(test_windows or [])
    buckets = [dict.fromkeys(BUCKET_TYPES + TEST_KEYS, 0) for _ in range(hours)]
    totals = dict.fromkeys(BUCKET_TYPES, 0)
    after_ack = set()                       # buckets con algo posterior al ACK
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
        detail = ev.get("detail") or {}
        if typ == "boot_loop" and detail.get("phase") == "end":
            continue                        # cuenta el inicio, no el fin
        i = min(hours - 1, int((t - start) // 3600))
        if typ == "test":
            if detail.get("phase") == "result":
                buckets[i]["test"] += 1
                if (detail.get("case") or {}).get("result") in testrun.FAILED:
                    buckets[i]["test_fail"] += 1
            continue
        if typ in ("boot", "panic", "boot_loop") and testrun.in_windows(t, wins):
            if typ != "boot_loop":          # el loop de un multietapa: sus boots ya se contaron
                buckets[i]["test_" + typ] += 1
            continue
        if typ in BUCKET_TYPES:
            buckets[i][typ] += 1
            if ack is None or t > ack:
                totals[typ] += 1
                after_ack.add(i)
        if typ in NOTABLE:
            recent.append({"ts": ev["ts"], "type": typ, "detail": ev.get("detail") or {}})
    acked = 0 if ack is None else max(0, min(hours, math.ceil((ack - start) / 3600)))
    if acked and ack is not None and (acked - 1) in after_ack:
        acked -= 1
    return {"buckets": buckets, "totals": totals, "acked": acked, "recent": recent[:20]}


def activity(devices: List[dict], hours: int = 24, now: Optional[float] = None) -> dict:
    """Por placa (las de /api/devices con MAC): actividad de las últimas `hours` horas."""
    now = time.time() if now is None else now
    out = {}
    for d in devices:
        if not d.get("mac"):
            continue
        a = device_activity(paths.device_events_file(d["mac"]), hours, now, _epoch(d.get("ack_at")),
                            testrun.windows(d["mac"], now))
        out[d["tty_name"]] = {"key": d.get("device_key") or d.get("sn") or d["mac"], "mac": d["mac"], **a}
    return {"now": events.iso_ms(now), "hours": hours, "devices": out}

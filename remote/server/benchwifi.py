"""
benchwifi.py — el WiFi del banco (docs/specs/bench-wifi.md): un hotspot en un dongle USB para las placas,
que se prende y apaga por comando (`espbench wifi on|off`, `GET/POST /api/wifi`).

- Lo hace el helper `espbench-wifi` (root, NetworkManager): el api lo llama con `sudo -n`. `runner` es el
  único lugar que ejecuta algo; los tests lo reemplazan.
- Sin `wifi.conf` (una Pi sin dongle) el bench no tiene WiFi: `status()` da `available: False`, `change()`
  da `unsupported` y nunca se llama al helper.
- Estado pedido (prendida, hasta cuándo, quién) en `meta/wifi_state.json`. Lo que vale es el helper: `on` es
  si la conexión de NM está activa. Prendida vence (default 8 h, máx 12 h): un timer la apaga, y al arrancar
  el api (`resume`) se apaga lo vencido; al bootear, NM no la levanta (autoconnect=false) y el estado se limpia.
- Cambiarla es tocar la red de todas las placas del bench: si una está reservada por otro, `locked` (salvo
  `force`). Cada cambio es un evento `wifi` en cada placa, con el cursor del fin de su log.
"""
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from server import events, locks, paths, runstate, taglog

TAG = "benchwifi"
HELPER = ["sudo", "-n", "/usr/local/bin/espbench-wifi"]
DEFAULT_ON_S = 8 * 3600
MAX_ON_S = 12 * 3600
NO_WIFI = "este bench no tiene WiFi (falta wifi.conf: enchufar el dongle y correr 'sudo espbench-wifi setup')"


class WifiError(Exception):
    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status, self.error, self.message = status, error, message


def _run(args: List[str], input: Optional[str] = None) -> Tuple[int, str, str]:
    """El helper con sudo. `input`: lo que va por stdin (configure: SSID y clave nunca van como argumentos)."""
    try:
        r = subprocess.run(HELPER + args, input=input, capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)
    return r.returncode, r.stdout, r.stderr


runner: Callable[..., Tuple[int, str, str]] = _run
SYS_ROOT = "/"          # tests: un /sys y /proc falsos
_lock = threading.Lock()
_timer: Optional[threading.Timer] = None


# ---------- config y estado ----------

def _uplinks(root: pathlib.Path) -> set:
    """Interfaces con la ruta default (/proc/net/route): la salida del bench a la red."""
    out = set()
    try:
        for line in (root / "proc/net/route").read_text().splitlines()[1:]:
            parts = line.split()
            if len(parts) > 2 and parts[1] == "00000000":
                out.add(parts[0])
    except OSError:
        pass
    return out


def candidates(root: Optional[str] = None) -> List[str]:
    """Dongles para el hotspot: interfaces WiFi en USB que no son la salida del bench. La misma regla que
    `espbench-wifi candidates`. El WiFi interno de una Pi (SDIO, y además su salida) no cuenta: en las Pi
    sin dongle esto da [] y la función WiFi no aparece. Sin sudo: solo lee /sys y /proc."""
    r = pathlib.Path(root or SYS_ROOT)
    net = r / "sys/class/net"
    up = _uplinks(r)
    out = []
    try:
        names = sorted(p.name for p in net.iterdir())
    except OSError:
        return []
    for name in names:
        d = net / name
        if name in up or not ((d / "wireless").exists() or (d / "phy80211").exists()):
            continue
        try:
            if "/usb" in os.path.realpath(d / "device"):
                out.append(name)
        except OSError:
            continue
    return out

def read_conf() -> Optional[Dict[str, str]]:
    """wifi.conf (IFACE, SSID, CHANNEL) o None si el bench no tiene WiFi."""
    try:
        text = paths.wifi_conf().read_text(encoding="utf-8")
    except OSError:
        return None
    conf = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip():
            conf[k.strip()] = v.strip()
    return conf if conf.get("IFACE") and conf.get("SSID") else None


def _load_state() -> dict:
    try:
        st = json.loads(paths.wifi_state_file().read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    path = paths.wifi_state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.chmod(tmp, 0o666)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return dt.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


# ---------- status del helper ----------

_STATION_RE = re.compile(r"^Station ([0-9A-Fa-f:]{17})")
_NUM_RE = re.compile(r"-?\d+")


def parse_status(text: str) -> dict:
    """Salida de `espbench-wifi status`: key=value, `--- stations` (iw station dump) y `--- leases`
    (dnsmasq: "<vence> <mac> <ip> <hostname> <client-id>")."""
    info: Dict[str, str] = {}
    stations: List[dict] = []
    leases: List[dict] = []
    section, cur = None, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("--- "):
            section = line[4:].strip()
            continue
        if section is None:
            k, sep, v = line.partition("=")
            if sep:
                info[k.strip()] = v.strip()
        elif section == "stations":
            m = _STATION_RE.match(line)
            if m:
                cur = {"mac": m.group(1).upper(), "signal_dbm": None, "connected_s": None}
                stations.append(cur)
            elif cur is not None:
                k, _, v = line.partition(":")
                n = _NUM_RE.search(v)
                if k == "signal" and n:
                    cur["signal_dbm"] = int(n.group())
                elif k == "connected time" and n:
                    cur["connected_s"] = int(n.group())
        elif section == "leases":
            parts = line.split()
            if len(parts) >= 4:
                leases.append({"mac": parts[1].upper(), "ip": parts[2],
                               "hostname": None if parts[3] == "*" else parts[3]})
    channel = info.get("channel")
    return {"available": info.get("available") == "1", "present": info.get("present") == "1",
            "active": info.get("active") == "1", "iface": info.get("iface"), "ssid": info.get("ssid"),
            "channel": int(channel) if channel and channel.isdigit() else None,
            "stations": stations, "leases": leases}


def status(boards: Optional[Dict[str, str]] = None) -> dict:
    """Lo que muestra GET /api/wifi. `boards`: MAC (sin separadores, mayúsculas) → nombre de la placa.
    Nunca la clave."""
    conf = read_conf()
    if conf is None:
        found = candidates()
        if not found:
            return {"available": False, "on": False}
        return {"available": True, "configured": False, "on": False, "iface": found[0], "ssid": None,
                "channel": None, "clients": [], "error": None}
    rc, out, err = runner(["status"])
    if rc != 0:
        return {"available": True, "on": None, "ssid": conf.get("SSID"), "clients": [],
                "error": f"espbench-wifi status falló: {(err or '').strip() or rc}"}
    s = parse_status(out)
    st = _load_state() if s["active"] else {}
    by_mac = {lease["mac"]: lease for lease in s["leases"]}
    names = boards or {}
    clients = [{"mac": c["mac"], "ip": by_mac.get(c["mac"], {}).get("ip"), "signal_dbm": c["signal_dbm"],
                "connected_s": c["connected_s"], "board": names.get(locks.normalize_mac(c["mac"]))}
               for c in s["stations"]]
    return {"available": True, "configured": True, "present": s["present"], "on": s["active"],
            "ssid": s["ssid"] or conf.get("SSID"), "channel": s["channel"],
            "until": _iso(st.get("until")), "by": st.get("by"), "since": _iso(st.get("since")),
            "clients": clients, "error": None if s["present"] else "el dongle no está enchufado"}


# ---------- cambios ----------

def reserved_by_others(lock_user: str, lock_token: str, now: Optional[float] = None) -> List[Tuple[str, "locks.Lock"]]:
    """Placas del bench con una reserva que no es de este par: cambiar el WiFi les cambia la red."""
    out = []
    for tty in runstate.list_all():
        lock = locks.read(tty, now)
        if lock is not None and lock.reservation and not lock.owned_by(lock_user, lock_token):
            out.append((tty, lock))
    return out


def _record_all(detail: dict) -> Dict[str, str]:
    """Evento `wifi` en cada placa del bench (al fin de su log). {placa: cursor}."""
    out = {}
    for tty, state in runstate.list_all().items():
        log_path = state.get("log_path")
        if not log_path:
            continue
        try:
            ev = events.record(log_path, "wifi", detail)
        except OSError:
            continue
        if ev:
            out[locks.normalize_mac(state.get("mac")) or tty] = ev["cursor"]
    return out


def _schedule(st: dict) -> None:
    """Timer que la apaga al vencer (en el proceso del api)."""
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None
    if st.get("on") and st.get("until"):
        _timer = threading.Timer(max(0.0, st["until"] - time.time()), expire_due)
        _timer.daemon = True
        _timer.start()


def change(action: str, for_s=None, *, user: Optional[str] = None, lock_user: str = "", lock_token: str = "",
           force: bool = False, forced_by: Optional[dict] = None, boards: Optional[Dict[str, str]] = None,
           now: Optional[float] = None) -> dict:
    """POST /api/wifi: on [for_s] | off. WifiError(status, error, mensaje) si no se puede."""
    now = time.time() if now is None else now
    if action not in ("on", "off"):
        raise WifiError(400, "bad_request", "action tiene que ser on u off")
    if read_conf() is None:
        if candidates():
            raise WifiError(409, "not_configured", "la red del bench no está configurada: dashboard → WiFi → Configurar")
        raise WifiError(409, "unsupported", NO_WIFI)
    until = None
    if action == "on":
        try:
            secs = DEFAULT_ON_S if for_s in (None, "") else float(for_s)
        except (TypeError, ValueError):
            raise WifiError(400, "bad_request", "for_s tiene que ser un número de segundos")
        if not 1 <= secs <= MAX_ON_S:
            raise WifiError(400, "bad_request", f"for_s entre 1 s y {MAX_ON_S // 3600} h")
        until = now + secs
    elif for_s not in (None, ""):
        raise WifiError(400, "bad_request", "off no lleva duración")
    blockers = reserved_by_others(lock_user, lock_token, now)
    if blockers and not force:
        tty, lock = blockers[0]
        raise WifiError(423, "locked", f"la placa de {tty} está reservada por '{lock.user}' hasta "
                                       f"{lock.expires_iso()}: cambiar el WiFi le cambia la red")
    with _lock:
        rc, _, err = runner(["up" if action == "on" else "down"])
        if rc != 0:
            raise WifiError(502, "unexpected", f"espbench-wifi {'up' if action == 'on' else 'down'} falló: "
                                               f"{(err or '').strip() or rc}")
        st = {"on": action == "on", "until": until, "by": user, "since": now}
        _save_state(st)
        _schedule(st)
    taglog.info(TAG, f"wifi {action}" + (f" hasta {_iso(until)}" if until else "") + f" ({user or '?'})")
    detail = {"action": action, "until": _iso(until), "user": user}
    if blockers:
        detail.update(forced_by or {"forced": True})
    r = status(boards)
    r["events"] = _record_all(detail)
    return r


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def configure(ssid, password=None, channel=None, *, user: Optional[str] = None, lock_user: str = "",
              lock_token: str = "", force: bool = False, forced_by: Optional[dict] = None,
              boards: Optional[Dict[str, str]] = None, now: Optional[float] = None) -> dict:
    """POST /api/wifi/config: SSID, clave (vacía = la que ya estaba) y canal de la red del bench. La clave va al
    helper por stdin y se guarda solo en el keyfile de NM: nunca vuelve (ni acá, ni en eventos, ni en logs)."""
    now = time.time() if now is None else now
    conf = read_conf()
    if conf is None and not candidates():
        raise WifiError(409, "unsupported", NO_WIFI)
    ssid = ssid if isinstance(ssid, str) else ""
    if not 1 <= len(ssid.encode("utf-8")) <= 32 or _CTRL_RE.search(ssid):
        raise WifiError(400, "bad_request", "el SSID tiene que tener entre 1 y 32 caracteres, sin saltos de línea")
    password = password or ""
    if not isinstance(password, str):
        raise WifiError(400, "bad_request", "clave inválida")
    if password:
        if not 8 <= len(password) <= 63 or any(not 32 <= ord(c) <= 126 for c in password):
            raise WifiError(400, "bad_request", "la clave tiene que tener entre 8 y 63 caracteres comunes (ASCII)")
    elif conf is None:
        raise WifiError(400, "bad_request", "falta la clave (la red todavía no tiene una)")
    if channel in (None, ""):
        channel = None
    else:
        try:
            channel = int(channel)
        except (TypeError, ValueError):
            channel = 0
        if not 1 <= channel <= 13:
            raise WifiError(400, "bad_request", "canal entre 1 y 13")
    on = False
    if conf is not None:
        rc, out, _ = runner(["status"])
        on = rc == 0 and parse_status(out)["active"]
    blockers = reserved_by_others(lock_user, lock_token, now) if on else []
    if blockers and not force:
        tty, lock = blockers[0]
        raise WifiError(423, "locked", f"la placa de {tty} está reservada por '{lock.user}' hasta "
                                       f"{lock.expires_iso()}: cambiar la red prendida le corta la conexión")
    with _lock:
        rc, out, err = runner(["configure"], input=f"{ssid}\n{password}\n{channel or ''}\n")
    if rc != 0:
        msg = (err or "").strip().removeprefix("espbench-wifi: ") or f"espbench-wifi configure falló ({rc})"
        raise WifiError(400, "bad_request", msg)
    taglog.info(TAG, f"wifi configurada: canal {channel or '-'} ({user or '?'})")
    detail = {"action": "config", "ssid": ssid, "channel": channel, "user": user}
    if blockers:
        detail.update(forced_by or {"forced": True})
    r = status(boards)
    r["events"] = _record_all(detail)
    warning = next((l.strip() for l in (out or "").splitlines() if l.startswith("OJO")), None)
    if warning:
        r["warning"] = warning
    return r


def expire_due(now: Optional[float] = None) -> bool:
    """La apaga si venció. True si la apagó (el timer, y `resume` al arrancar el api)."""
    now = time.time() if now is None else now
    with _lock:
        st = _load_state()
        if not st.get("on") or st.get("until") is None or now < st["until"] - 0.5:
            return False
        rc, _, err = runner(["down"])
        if rc != 0:
            taglog.error(TAG, f"no se pudo apagar el WiFi vencido: {(err or '').strip() or rc}")
            return False
        _save_state({"on": False, "until": None, "by": None, "since": now})
    taglog.info(TAG, "wifi off: venció")
    _record_all({"action": "off", "reason": "vencida", "until": None, "user": None})
    return True


def resume(now: Optional[float] = None) -> None:
    """Al arrancar el api. Sin WiFi o apagada: nada. Vencida: la apaga. Si NM no la tiene arriba (se
    reinició el bench: autoconnect=false) el estado guardado ya no vale. Si sigue, vuelve el timer."""
    now = time.time() if now is None else now
    st = _load_state()
    if read_conf() is None or not st.get("on"):
        return
    if st.get("until") is not None and now >= st["until"]:
        expire_due(now)
        return
    rc, out, _ = runner(["status"])
    if rc == 0 and not parse_status(out)["active"]:
        _save_state({"on": False, "until": None, "by": None, "since": now})
        return
    _schedule(st)

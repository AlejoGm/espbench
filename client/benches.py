"""
benches.py — encontrar los benches de espbench y en cuál está cada placa.

Un bench es cualquier host que corre el dashboard de espbench (remote/server/api.py,
puerto 8080): una Raspberry Pi u otra máquina. Se buscan en dos fuentes:

- Tailscale: todos los peers online de `tailscale status --json`.
- Config: hosts listados a mano (benches fuera de la tailnet).

A cada candidato se le pide GET /api/version. Es bench si contesta
{"app": "espbench", ...}. Lo identifica la MAC de la máquina (`id`): el mismo
bench visto por LAN y por Tailscale cuenta una sola vez, y renombrarlo no lo
convierte en otro. El nombre (`name`) lo declara el bench y es para mostrar.

Solo stdlib: lo usan deploy.py, bench-master y el CLI de agentes.

Config (opcional): ~/.config/espbench-benches.json, o la ruta de ESPBENCH_BENCHES_CONFIG.
    {"tailscale": true, "hosts": ["192.168.1.50", "lab-bench:8080"], "timeout_s": 2}
"""
import concurrent.futures
import dataclasses
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from typing import Callable, List, Optional, Tuple

DASHBOARD_PORT = 8080
CONFIG_ENV = "ESPBENCH_BENCHES_CONFIG"
DEFAULT_CONFIG = pathlib.Path.home() / ".config" / "espbench-benches.json"
DEFAULT_TIMEOUT_S = 2.0
CACHE_TTL_S = 30.0       # scan_cached: la lista de benches (no sus devices) se reusa este tiempo

_TAILSCALE_PATHS = ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale")


@dataclasses.dataclass
class Candidate:
    """Un host que puede ser un bench. `label` es cómo lo conoce la fuente (hostname)."""
    address: str
    port: int = DASHBOARD_PORT
    label: str = ""
    source: str = "config"   # "config" | "tailscale"

    @property
    def url(self) -> str:
        host = f"[{self.address}]" if ":" in self.address else self.address
        return f"http://{host}:{self.port}"


@dataclasses.dataclass
class Bench:
    name: str
    url: str
    address: str
    port: int
    source: str
    version: Optional[str] = None
    ok: bool = False
    error: Optional[str] = None
    devices: List[dict] = dataclasses.field(default_factory=list)
    auth: Optional[bool] = None     # el bench tiene token de la API (/api/version)
    legacy: bool = False            # sin `app: espbench`: bench viejo (sin /api/board, reservas, notas)
    id: Optional[str] = None        # MAC de la máquina (/api/version); None en benches viejos

    @property
    def key(self) -> str:
        """Identidad: la MAC del host; sin ella (bench viejo), el nombre."""
        return self.id or "name:" + self.name


# ---------- config ----------

def load_config(path: Optional[str] = None) -> dict:
    p = pathlib.Path(path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)
    cfg = {"tailscale": True, "hosts": [], "timeout_s": DEFAULT_TIMEOUT_S}
    if p.exists():
        cfg.update(json.loads(p.read_text()))
    return cfg


def parse_host(entry: str) -> Candidate:
    """"host", "host:port" o "[ipv6]:port"."""
    entry = entry.strip()
    if entry.startswith("["):
        addr, _, rest = entry[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else DASHBOARD_PORT
        return Candidate(addr, port, label=addr)
    if entry.count(":") == 1:
        addr, port = entry.split(":")
        return Candidate(addr, int(port), label=addr)
    return Candidate(entry, DASHBOARD_PORT, label=entry)


# ---------- tailscale ----------

def _tailscale_bin() -> Optional[str]:
    for p in _TAILSCALE_PATHS:
        found = shutil.which(p) or (p if os.path.isfile(p) and os.access(p, os.X_OK) else None)
        if found:
            return found
    return None


def tailscale_status() -> Optional[dict]:
    """`tailscale status --json`, o None si no hay tailscale o no está conectado."""
    exe = _tailscale_bin()
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def tailscale_candidates(status: Optional[dict]) -> List[Candidate]:
    """Peers online de la tailnet. Los offline no se prueban: sería esperar un timeout
    por cada uno. Se usa la IP de Tailscale, que anda aunque no haya MagicDNS."""
    out = []
    for peer in ((status or {}).get("Peer") or {}).values():
        ips = peer.get("TailscaleIPs") or []
        if not peer.get("Online") or not ips:
            continue
        ipv4 = [ip for ip in ips if ":" not in ip]
        out.append(Candidate((ipv4 or ips)[0], DASHBOARD_PORT, label=peer.get("HostName", ""),
                             source="tailscale"))
    return out


# ---------- sondeo ----------

def http_get_json(url: str, timeout: float):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def probe(c: Candidate, timeout: float, get_json: Callable = http_get_json) -> Optional[Bench]:
    """Bench si el host contesta como espbench; None si no (no es un bench o no responde).

    Un bench sin actualizar contesta solo {"version": ...} (o {"version", "auth"}, los
    de la rama de agentes): se lo acepta igual (`legacy`: bench-master y deploy.py lo
    muestran, el CLI `espbench` lo ignora) y se lo nombra como lo conoce la fuente."""
    try:
        info = get_json(c.url + "/api/version", timeout)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if not isinstance(info, dict):
        return None
    legacy = "version" in info and set(info) <= {"version", "auth"}
    if info.get("app") != "espbench" and not legacy:
        return None
    return Bench(name=str(info.get("name") or c.label or c.address), url=c.url, address=c.address,
                 port=c.port, source=c.source, version=info.get("version"), ok=True,
                 auth=info.get("auth") if isinstance(info.get("auth"), bool) else None, legacy=legacy,
                 id=str(info["id"]).lower() if info.get("id") else None)


def fetch_devices(b: Bench, timeout: float, get_json: Callable = http_get_json) -> Bench:
    try:
        devices = get_json(b.url + "/api/devices", timeout)
        b.devices = devices if isinstance(devices, list) else []
    except (OSError, ValueError, urllib.error.URLError) as e:
        b.ok = False
        b.error = f"/api/devices: {e}"
    return b


def candidates(cfg: dict, status: Optional[dict] = None) -> List[Candidate]:
    """Primero los de la config (si un bench está en las dos fuentes, gana su entrada
    explícita), después los de Tailscale."""
    out = [dataclasses.replace(parse_host(h), source="config") for h in cfg.get("hosts") or []]
    if cfg.get("tailscale", True):
        out += tailscale_candidates(status if status is not None else tailscale_status())
    return out


def scan(cfg: Optional[dict] = None, status: Optional[dict] = None,
         get_json: Callable = http_get_json) -> List[Bench]:
    """Todos los benches que contestan, con sus devices. Sondea en paralelo."""
    cfg = cfg if cfg is not None else load_config()
    timeout = float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S))
    cands = candidates(cfg, status)
    if not cands:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(cands))) as ex:
        found = list(ex.map(lambda c: probe(c, timeout, get_json), cands))
        benches, seen = [], set()
        for b in found:
            if b and b.key not in seen:          # el mismo host por LAN y por Tailscale cuenta una vez
                seen.add(b.key)
                benches.append(b)
        list(ex.map(lambda b: fetch_devices(b, timeout, get_json), benches))
    return benches


_CACHED_FIELDS = ("name", "url", "address", "port", "source", "version", "auth", "legacy", "id")


def _load_cache(path: pathlib.Path, ttl_s: float, now: float) -> Optional[List[Bench]]:
    try:
        data = json.loads(path.read_text())
        if now - float(data["ts"]) > ttl_s or now < float(data["ts"]):
            return None
        return [Bench(**{k: b.get(k) for k in _CACHED_FIELDS}, ok=True) for b in data["benches"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_cache(path: pathlib.Path, found: List[Bench], now: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".benches.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump({"ts": now, "benches": [{k: getattr(b, k) for k in _CACHED_FIELDS} for b in found]}, f)
        os.replace(tmp, path)
    except OSError:
        pass        # sin cache: el próximo comando escanea de nuevo


def scan_cached(cache_path, ttl_s: float = CACHE_TTL_S, fresh: bool = False, cfg: Optional[dict] = None,
                status: Optional[dict] = None, get_json: Callable = http_get_json,
                now: Callable[[], float] = time.time) -> Tuple[List[Bench], bool]:
    """Como scan(), pero la lista de benches (qué hosts son benches) se guarda en
    `cache_path` y se reusa por `ttl_s`: el scan sondea cada peer de la tailnet
    (hasta timeout_s por peer que no contesta). Los devices se piden siempre de
    nuevo. Devuelve (benches, salió_de_la_cache)."""
    cache_path = pathlib.Path(cache_path)
    cfg = cfg if cfg is not None else load_config()
    if not fresh:
        cached = _load_cache(cache_path, ttl_s, now())
        if cached is not None:
            timeout = float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S))
            if cached:
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(cached))) as ex:
                    list(ex.map(lambda b: fetch_devices(b, timeout, get_json), cached))
            return cached, True
    found = scan(cfg, status, get_json)
    _save_cache(cache_path, found, now())
    return found, False


# ---------- resolve ----------

def _norm_mac(s: str) -> str:
    return s.upper().replace(":", "").replace("-", "")


def device_matches(key: str, bench: str, d: dict) -> bool:
    """`key` = device_key, SN, MAC (con o sin separadores), tty, "<bench>/<tty>"
    o "<placa>@<bench>" (cualquiera de las anteriores, solo en ese bench)."""
    k = key.strip()
    if not k:
        return False
    if "@" in k:
        dev, _, b = k.rpartition("@")
        if b == bench and dev and device_matches(dev, bench, d):
            return True
    if "/" in k:
        b, _, tty = k.partition("/")
        return b == bench and tty in (d.get("tty_name"), d.get("tty"))
    if k in (d.get("tty_name"), d.get("tty")):
        return True
    if d.get("device_key") and d["device_key"].lower() == k.lower():
        return True
    if d.get("sn") and d["sn"].lower() == k.lower():
        return True
    return bool(d.get("mac")) and _norm_mac(d["mac"]) == _norm_mac(k)


def find(key: str, benches: List[Bench]) -> List[Tuple[Bench, dict]]:
    return [(b, d) for b in benches for d in b.devices if device_matches(key, b.name, d)]


class ResolveError(LookupError):
    """`kind`: "not_found" o "ambiguous"; `hits`: los (bench, device) del ambiguo."""

    def __init__(self, message: str, kind: str = "not_found", hits: Optional[list] = None):
        super().__init__(message)
        self.kind = kind
        self.hits = hits or []


def resolve(key: str, benches: Optional[List[Bench]] = None) -> Tuple[Bench, dict]:
    """El único (bench, device) que corresponde a `key`. ResolveError si no hay o hay varios."""
    benches = benches if benches is not None else scan()
    hits = find(key, benches)
    if not hits:
        names = ", ".join(b.name for b in benches) or "ninguno"
        raise ResolveError(f"'{key}' no está en ningún bench (benches encontrados: {names})")
    if len(hits) > 1:
        where = ", ".join(f"{b.name}/{d.get('tty_name')}" for b, d in hits)
        raise ResolveError(f"'{key}' es ambiguo, aparece en: {where}. Usá '<placa>@<bench>' o '<bench>/<tty>'",
                           "ambiguous", hits)
    return hits[0]

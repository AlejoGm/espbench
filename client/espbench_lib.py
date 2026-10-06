#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
espbench_lib.py — cliente de espbench para agentes (y para el CLI `espbench`).

Spec: docs/specs/agents-cli.md §8. Sin input(), sin rich, sin print: los
mensajes de progreso van a un callback (`log`). HTTP con urllib (sin
dependencias). Corre en la Mac del dev: Python 3.9+.

- Config: flags > env (ESPBENCH_HOST, ESPBENCH_TOKEN, ESPBENCH_USER,
  ESPBENCH_LOCK_TOKEN) > ~/.config/espbench.json (perfiles) > .flashcfg.json.
- Client.resolve(name): device_key, SN, MAC o tty, vía /api/devices.
- Client.read_range(...): rango del log con la espera del lado del cliente
  (poll desde `end`, `idle:`, `--for`, `--timeout`, eco). El `until` lo evalúa
  solo el server (§5, D6).
- Client.flash(...) / verify(...): flash por TCP (protocol.py) y verificación
  del arranque con ventana de asentamiento (§8.2, §12.1).
- collect_artifact / flash_one: el camino del flash con flasher_args.json del
  build dir. deploy.py los importa de acá.

Los errores llevan `error` estable (el contrato, §8.3) y su exit code.
"""
import dataclasses
import json
import os
import pathlib
import re
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Callable, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from common import recv_msg, send_msg, sha256_file  # noqa: E402

DASHBOARD_PORT = 8080
POLL_S = 0.3
HTTP_TIMEOUT_S = 15.0
DEFAULT_TIMEOUT_S = 30.0         # espera de until / idle sin --timeout
VERIFY_WINDOW_S = 10.0           # ventana de asentamiento de --verify
VERIFY_TIMEOUT_S = 60.0          # hasta el primer boot después del flash/reset
READY_TIMEOUT_S = 10.0           # state == monitoring después del flash
DEFAULT_MAX_LINES = 200
HEAD_LINES = 50
CRASH_TYPES = ("panic", "boot_loop")

EXIT_CODES = {
    None: 0,
    "unexpected": 1, "bad_request": 1,
    "flash_failed": 2,
    "crashed": 3,
    "timeout": 4,
    "busy": 5,
    "locked": 6, "reservation_lost": 6, "token_mismatch": 6,
    "not_found": 7, "device_changed": 7, "session_down": 7,
    "bad_anchor": 8, "cursor_expired": 8,
    "session_ended": 9,
    "network": 10, "auth": 10, "auth_config": 10,
}

# Errores del protocolo TCP del flash (protocol.py) → contrato del cliente.
_FLASH_ERRORS = {
    "unauthorized": "auth",
    "auth_config": "auth_config",
    "device_locked": "locked",
    "token_mismatch": "token_mismatch",
    "lock_credentials_required": "bad_request",
    "device_busy": "busy",
    "device_changed": "device_changed",
    "bad_action": "unexpected",
}

# Respuestas HTTP sin {"detail": {"error"}} (endpoints viejos, validación de FastAPI).
_HTTP_ERRORS = {400: "bad_request", 401: "auth", 403: "token_mismatch", 404: "not_found", 409: "busy",
                410: "cursor_expired", 422: "bad_request", 423: "locked"}

_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)?")
_DUR_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}
_CURSOR_RE = re.compile(r"c:(\d{8}_\d{6}_\d+):(\d+)")
_TIME_LINE_RE = re.compile(r"\d\d:\d\d:\d\d\.\d{3} ")


class EspbenchError(Exception):
    """Error con `error` estable (§8.3). `data`: lo que se alcanzó a juntar."""

    def __init__(self, error: str, message: str, status: Optional[int] = None, data: Optional[dict] = None):
        super().__init__(message)
        self.error = error
        self.message = message
        self.status = status
        self.data = data or {}

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.error, 1)

    def to_dict(self) -> dict:
        return {**self.data, "ok": False, "error": self.error, "message": self.message}


def exit_code(error: Optional[str]) -> int:
    return EXIT_CODES.get(error, 1)


def parse_duration(text, what: str = "duración") -> float:
    """300ms, 10s, 5m, 2h, 1d o un número (segundos)."""
    if isinstance(text, (int, float)):
        return float(text)
    m = _DUR_RE.fullmatch(str(text or "").strip())
    if not m:
        raise EspbenchError("bad_request", f"{what} inválida: {text!r} (ej. 300ms, 10s, 5m)")
    return float(m.group(1)) * _DUR_UNITS[m.group(2)]


def parse_cursor(cursor: Optional[str]):
    m = _CURSOR_RE.fullmatch(cursor or "")
    return (m.group(1), int(m.group(2))) if m else None


def bare_mac(mac: Optional[str]) -> Optional[str]:
    if not mac:
        return None
    return mac.upper().replace(":", "").replace("-", "")


# ---------- config ----------

def _read_json(path: pathlib.Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise EspbenchError("bad_request", f"no se pudo leer {path}: {e}")
    return data if isinstance(data, dict) else None


def find_flashcfg(start: Optional[pathlib.Path] = None) -> Optional[pathlib.Path]:
    """.flashcfg.json del proyecto: en el directorio actual o el primero hacia arriba."""
    d = pathlib.Path(start or os.getcwd()).resolve()
    for cand in [d] + list(d.parents):
        p = cand / ".flashcfg.json"
        if p.is_file():
            return p
    return None


def _flashcfg_remote(cfg: dict, device: Optional[str]) -> dict:
    """La entrada de `remote` de un .flashcfg.json (dict o lista). Con lista, la
    que se llama como `device` (name o device_key); si no, la primera."""
    r = cfg.get("remote")
    if isinstance(r, dict):
        return r
    if isinstance(r, list) and r:
        for entry in r:
            if isinstance(entry, dict) and device and device in (entry.get("name"), entry.get("device_key")):
                return entry
        return r[0] if isinstance(r[0], dict) else {}
    return {}


@dataclasses.dataclass
class Config:
    host: Optional[str] = None
    token: str = ""
    lock_user: str = ""
    lock_token: str = ""
    profile: Optional[str] = None
    flashcfg: dict = dataclasses.field(default_factory=dict)      # .flashcfg.json entero (chip, baud...)
    flashcfg_path: Optional[pathlib.Path] = None
    sources: dict = dataclasses.field(default_factory=dict)       # campo → de dónde salió

    FIELDS = ("host", "token", "lock_user", "lock_token")
    ENV = {"host": "ESPBENCH_HOST", "token": "ESPBENCH_TOKEN", "lock_user": "ESPBENCH_USER",
           "lock_token": "ESPBENCH_LOCK_TOKEN"}

    @classmethod
    def load(cls, host: Optional[str] = None, profile: Optional[str] = None, token: Optional[str] = None,
             device: Optional[str] = None, env: Optional[dict] = None, cwd: Optional[pathlib.Path] = None,
             user_config: Optional[pathlib.Path] = None) -> "Config":
        """Precedencia: flags > env > ~/.config/espbench.json (perfil) > .flashcfg.json."""
        env = os.environ if env is None else env
        layers = [("flag", {"host": host, "token": token})]
        layers.append(("env", {k: env.get(v) for k, v in cls.ENV.items()}))

        ucfg_path = pathlib.Path(user_config or env.get("ESPBENCH_CONFIG")
                                 or pathlib.Path.home() / ".config" / "espbench.json")
        ucfg = _read_json(ucfg_path) or {}
        profile = profile or env.get("ESPBENCH_PROFILE") or ucfg.get("default_profile")
        profiles = ucfg.get("profiles") or {}
        if profile is not None and profile not in profiles:
            raise EspbenchError("bad_request", f"no hay perfil '{profile}' en {ucfg_path}")
        if profile is not None:
            layers.append((f"profile:{profile}", profiles[profile]))
        layers.append(("user_config", {k: ucfg.get(k) for k in cls.FIELDS}))

        fpath = find_flashcfg(cwd)
        fcfg = (_read_json(fpath) or {}) if fpath else {}
        layers.append((".flashcfg.json", _flashcfg_remote(fcfg, device)))

        out = cls(profile=profile, flashcfg=fcfg, flashcfg_path=fpath)
        for field in cls.FIELDS:
            for source, values in layers:
                value = values.get(field) if isinstance(values, dict) else None
                if value not in (None, ""):
                    setattr(out, field, str(value).strip())
                    out.sources[field] = source
                    break
        return out

    @property
    def base_url(self) -> str:
        if not self.host:
            raise EspbenchError("bad_request", "falta el host de la Pi: --host, ESPBENCH_HOST, "
                                               "~/.config/espbench.json o remote.host en .flashcfg.json")
        h = self.host.rstrip("/")
        if "://" in h:
            return h
        if ":" in h and not h.startswith("["):
            return f"http://{h}"
        return f"http://{h}:{DASHBOARD_PORT}"

    @property
    def hostname(self) -> str:
        """Host sin esquema ni puerto (el flash va por TCP al puerto del device)."""
        return urllib.parse.urlsplit(self.base_url).hostname or ""

    def creds(self) -> dict:
        if not self.lock_user or not self.lock_token:
            return {}
        return {"lock_user": self.lock_user, "lock_token": self.lock_token}

    def require_creds(self, what: str) -> dict:
        c = self.creds()
        if not c:
            raise EspbenchError("bad_request", f"{what} necesita lock_user y lock_token (ESPBENCH_USER / "
                                               "ESPBENCH_LOCK_TOKEN, perfil o remote de .flashcfg.json)")
        return c


# ---------- placas ----------

@dataclasses.dataclass
class Board:
    """Una placa resuelta. `key` es lo que va en /api/board/{key} (la MAC si se
    conoce: no cambia con el tty ni con un rename)."""
    name: str
    key: str
    mac: Optional[str] = None
    sn: Optional[str] = None
    device_key: Optional[str] = None
    tty: Optional[str] = None
    state: Optional[str] = None
    port_tcp: Optional[int] = None
    lock_user: Optional[str] = None
    lock_expires: Optional[str] = None
    hw_model: Optional[str] = None
    info: dict = dataclasses.field(default_factory=dict)

    @property
    def live(self) -> bool:
        return bool(self.tty) and self.state not in (None, "disconnected")

    @property
    def label(self) -> str:
        return self.device_key or self.sn or self.mac or self.name

    @classmethod
    def from_info(cls, name: str, d: dict) -> "Board":
        mac = d.get("mac")
        return cls(name=name, key=bare_mac(mac) or name, mac=mac, sn=d.get("sn"),
                   device_key=d.get("device_key"), tty=d.get("tty_name"), state=d.get("state"),
                   port_tcp=d.get("port_tcp"), lock_user=d.get("lock_user"),
                   lock_expires=d.get("lock_expires"), hw_model=d.get("hw_model"), info=d)


def summarize_device(d: dict) -> dict:
    """Lo que le sirve a un agente de /api/devices, compacto (tokens)."""
    health = d.get("health") or {}
    out = {
        "key": d.get("device_key") or d.get("sn") or d.get("mac") or d.get("tty_name"),
        "device_key": d.get("device_key"), "sn": d.get("sn"), "mac": d.get("mac"),
        "tty": d.get("tty_name"), "state": d.get("state"),
        "lock_user": d.get("lock_user"), "lock_expires": d.get("lock_expires"),
        "hw_model": d.get("hw_model"), "fw_project": d.get("fw_project"), "fw_version": d.get("fw_version"),
    }
    if health:
        out["health"] = {k: health.get(k) for k in ("boots", "panics", "boot_loop") if k in health}
    return out


def _match_device(name: str, d: dict) -> bool:
    bare = bare_mac(name)
    tty = d.get("tty_name")
    return (name in (d.get("device_key"), d.get("sn"), tty, d.get("tty"), f"/dev/{tty}")
            or (bool(d.get("mac")) and bare_mac(d.get("mac")) == bare))


# ---------- artefacto y flash por TCP (antes en deploy.py) ----------

def collect_artifact(build_dir: pathlib.Path, include_elf: bool = True,
                     log: Optional[Callable[[str], None]] = None) -> pathlib.Path:
    """ZIP con flasher_args.json del build dir + los .bin que nombra (+ el .elf
    como firmware.elf, para decodificar backtraces en la Pi). Devuelve la ruta
    del zip, en un directorio temporal propio."""
    log = log or (lambda msg: None)
    build_dir = pathlib.Path(build_dir)
    log("Recolectando archivos para flashear...")
    fa = build_dir / "flasher_args.json"
    if not fa.exists():
        raise EspbenchError("bad_request", f"no existe {fa} (corré un build primero)")
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="artifact_"))
    out = tmpdir / "artifact.zip"
    log(f"Creando ZIP en {out}")

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        copied = set()

        def add(p: pathlib.Path):
            p = p.resolve()
            if p.is_file() and p not in copied:
                z.write(p, arcname=p.name)
                copied.add(p)
                log(f"+ {p.name} ({p.stat().st_size} bytes)")

        def add_rel(path):
            if path:
                bp = pathlib.Path(path)
                add(bp if bp.is_absolute() else build_dir / bp)

        z.write(fa, arcname="flasher_args.json")
        log("+ flasher_args.json")
        J = json.loads(fa.read_text(encoding="utf-8"))

        ff = J.get("flash_files")
        if isinstance(ff, dict):
            log(f"flash_files es dict con {len(ff)} entradas")
            for _off, path in ff.items():
                add_rel(path)
        elif isinstance(ff, list):
            log(f"flash_files es list con {len(ff)} entradas")
            for it in ff:
                path = None
                if isinstance(it, (list, tuple)) and len(it) >= 2:
                    path = it[1]
                elif isinstance(it, dict):
                    path = it.get("file") or it.get("bin_file") or it.get("path")
                add_rel(path)

        for key in ["bootloader", "app", "partition-table", "otadata"]:
            entry = J.get(key)
            if isinstance(entry, dict):
                add_rel(entry.get("file"))

        for rel in ["bootloader/bootloader.bin", "partition_table/partition-table.bin", "ota_data_initial.bin",
                    "clc1.bin", "app.bin"]:
            p = build_dir / rel
            if p.exists():
                add(p)

        if include_elf:
            elf_files = list(build_dir.glob("*.elf"))
            if elf_files:
                z.write(elf_files[0], arcname="firmware.elf")
                log(f"+ firmware.elf ({elf_files[0].stat().st_size} bytes)")
            else:
                log("WARNING: no .elf found in build dir, skipping")

    log(f"✓ Artifact creado: {out.name} ({out.stat().st_size / (1024 * 1024):.2f} MB)")
    return out


def hw_model_from_build(build_dir: pathlib.Path) -> Optional[str]:
    """hw_model de build/project_description.json (project_name hasta el último '-')."""
    desc = pathlib.Path(build_dir) / "project_description.json"
    if not desc.exists():
        return None
    try:
        pname = json.loads(desc.read_text()).get("project_name", "")
    except Exception:
        return None
    if not pname:
        return None
    idx = pname.rfind("-")
    return pname[:idx] if idx >= 0 else pname


def _remote_name(r: dict) -> str:
    return r.get("name") or r.get("device_key") or r.get("host", "?")


def flash_one(remote_cfg: dict, artifact: pathlib.Path, digest: str, size: int,
              job_id: str, chip: str, flash_baud: int, encrypt: bool, erase: bool,
              on_status: Optional[Callable[[str], None]] = None,
              on_line: Optional[Callable[[str], None]] = None,
              on_log: Optional[Callable[[str], None]] = None) -> dict:
    """Un pedido upload_and_flash por TCP (remote/server/protocol.py). Devuelve
    la respuesta final del server (+ name y logs), o {"ok": False, "error": ...}.
    Nunca levanta: un error de red queda con "network": True."""
    name = _remote_name(remote_cfg)
    logs = []

    def log(msg):
        logs.append(msg)
        if on_log:
            on_log(msg)

    def status(phase):
        if on_status:
            on_status(phase)
        log(f"  {phase}")

    lock_user = str(remote_cfg.get("lock_user", "")).strip()
    lock_token = str(remote_cfg.get("lock_token", "")).strip()
    if not lock_user or not lock_token:
        return {"ok": False, "name": name, "error": "falta lock_user/lock_token en config", "logs": logs}

    token = str(remote_cfg.get("token", ""))
    host = remote_cfg["host"]
    port = int(remote_cfg["port"])
    header = {
        "token": token, "action": "upload_and_flash",
        "job_id": f"{job_id}_{name}", "chip": chip, "baud": flash_baud,
        "encrypt": bool(encrypt), "erase": bool(erase),
        "artifact_size": size, "artifact_sha256": digest, "artifact_name": artifact.name,
        "lock_user": lock_user, "lock_token": lock_token,
        "stream": True,
    }
    try:
        status("conectando...")
        s = socket.create_connection((host, port), timeout=30)
        try:
            send_msg(s, header)
            status("esperando ACK...")
            ack = recv_msg(s)
            if not ack.get("ok") or ack.get("phase") != "ready":
                err = ack.get("message") or ack.get("error") or "ACK fallido"
                return {**ack, "ok": False, "name": name, "error": err, "code": ack.get("error"), "logs": logs}
            status(f"enviando artifact ({size / (1024 * 1024):.1f} MB)...")
            with artifact.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    s.sendall(chunk)
            status("flasheando...")
            s.settimeout(300)
            stream_lines = []
            while True:
                msg = recv_msg(s)
                if "ok" in msg:  # mensaje final (server nuevo o viejo)
                    resp = msg
                    break
                if msg.get("phase") == "log":
                    line = msg.get("line", "")
                    stream_lines.append(line)
                    if on_line:
                        on_line(line)
            resp.setdefault("name", name)
            if not resp.get("ok"):
                resp.setdefault("code", resp.get("error"))
            resp["logs"] = logs + stream_lines
            return resp
        finally:
            s.close()
    except Exception as e:
        return {"ok": False, "name": name, "error": str(e), "logs": logs,
                "network": isinstance(e, (OSError, ConnectionError))}


# ---------- rangos del log ----------

class _Range:
    """Junta los polls de una espera: líneas (con cabeza + cola como el server),
    eventos, el primer start y el último end."""

    def __init__(self, max_lines: int):
        self.max = max(1, max_lines)
        self.head_n = min(HEAD_LINES, self.max // 2)
        self.head: list = []
        self.tail: list = []
        self.count = 0
        self.date: Optional[str] = None
        self.start: Optional[str] = None
        self.end: Optional[str] = None
        self.events: list = []
        self.truncated = False
        self.last: dict = {}

    def add(self, r: dict) -> None:
        self.last = r
        if self.start is None:
            self.start = r.get("start")
        self.end = r.get("end") or self.end
        self.truncated = self.truncated or bool(r.get("truncated"))
        date = r.get("date")
        if self.date is None:
            self.date = date
        for line in r.get("lines") or []:
            # Cada poll lleva su `date`: una línea de otro día que el primero, con fecha
            if date and date != self.date and _TIME_LINE_RE.match(line):
                line = f"{date} {line}"
            self._push(line)
        seen = {(e.get("type"), e.get("cursor")) for e in self.events}
        self.events += [e for e in r.get("events") or [] if (e.get("type"), e.get("cursor")) not in seen]

    def _push(self, line: str) -> None:
        self.count += 1
        if len(self.head) < self.head_n:
            self.head.append(line)
            return
        self.tail.append(line)
        if len(self.tail) > self.max - self.head_n:
            self.tail.pop(0)

    def lines(self) -> list:
        omitted = self.count - len(self.head) - len(self.tail)
        if omitted <= 0:
            return self.head + self.tail
        self.truncated = True
        marker = "… 1 línea omitida …" if omitted == 1 else f"… {omitted} líneas omitidas …"
        return self.head + [marker] + self.tail


def _cursor_lt(a: Optional[str], b: Optional[str]) -> bool:
    pa, pb = parse_cursor(a), parse_cursor(b)
    return bool(pa and pb and pa[0] == pb[0] and pa[1] < pb[1])


# ---------- cliente ----------

class Client:
    """Un cliente contra una Pi. `log(msg)`: progreso para humanos (el CLI lo
    manda a stderr; con --json, a ningún lado)."""

    def __init__(self, config: Config, poll_s: float = POLL_S, log: Optional[Callable[[str], None]] = None,
                 http_timeout: float = HTTP_TIMEOUT_S, state_dir: Optional[pathlib.Path] = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.config = config
        self.poll_s = poll_s
        self.log = log or (lambda msg: None)
        self.http_timeout = http_timeout
        self.state_dir = pathlib.Path(state_dir or os.environ.get("ESPBENCH_STATE_DIR")
                                      or pathlib.Path.home() / ".cache" / "espbench")
        self._clock = clock
        self._sleep = sleep

    # ----- HTTP -----

    def request(self, method: str, path: str, query: Optional[dict] = None, body: Optional[dict] = None):
        url = self.config.base_url + path
        if query:
            q = {k: v for k, v in query.items() if v is not None}
            if q:
                url += "?" + urllib.parse.urlencode(q)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise _http_error(e)
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise EspbenchError("network", f"sin conexión con {self.config.base_url}: {reason}")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise EspbenchError("unexpected", f"respuesta no JSON de {path}")

    # ----- placas -----

    def devices(self) -> list:
        return self.request("GET", "/api/devices") or []

    def resolve(self, name: str, write: bool = False, need_mac: bool = True) -> Board:
        """device_key, SN, MAC o tty → Board, con /api/devices. Una placa que no
        está ahí (desconectada) se puede leer igual por key (las lecturas son
        por MAC en la Pi); para escribir tiene que estar viva. need_mac: el log
        y los eventos por placa necesitan la MAC."""
        name = (name or "").strip()
        if not name:
            raise EspbenchError("bad_request", "falta la placa (device_key, SN, MAC o tty)")
        found = [d for d in self.devices() if _match_device(name, d)]
        live = [d for d in found if d.get("state") not in (None, "disconnected")]
        pick = (live or found or [None])[0]
        if pick is None:
            if write:
                raise EspbenchError("not_found", f"no hay placa '{name}' conectada (ver `espbench ls`)")
            return Board(name=name, key=name)
        board = Board.from_info(name, pick)
        if write and not board.live:
            raise EspbenchError("not_found", f"la placa '{name}' está desconectada o sin proceso "
                                             f"({board.state or 'sin estado'})")
        if not board.mac and need_mac and not write:
            raise EspbenchError("not_found", f"la placa '{name}' todavía no tiene MAC: no hay log por placa")
        return board

    def _board_path(self, key: str, what: str) -> str:
        return f"/api/board/{urllib.parse.quote(key, safe='')}/{what}"

    def board_log(self, key: str, **params) -> dict:
        return self.request("GET", self._board_path(key, "log"), params)

    def board_events(self, key: str, types: Optional[str] = None, since: Optional[str] = None,
                     limit: Optional[int] = None, order: Optional[str] = None) -> dict:
        return self.request("GET", self._board_path(key, "events"),
                            {"type": types, "since": since, "limit": limit, "order": order})

    # ----- reservas (registro local: "este agente reservó esta placa") -----

    def _resv_path(self) -> pathlib.Path:
        return self.state_dir / "reservations.json"

    def _resv_key(self, board: Board) -> str:
        return f"{self.config.base_url}|{bare_mac(board.mac)}"

    def _resv_load(self) -> dict:
        try:
            data = json.loads(self._resv_path().read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _resv_save(self, data: dict) -> None:
        path = self._resv_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)

    def holds_reservation(self, board: Board) -> bool:
        rec = self._resv_load().get(self._resv_key(board))
        return bool(rec) and rec.get("user") == self.config.lock_user

    def _write_body(self, board: Board, **extra) -> dict:
        """expect_mac + par del lock (+ require_reservation si este cliente
        reservó la placa). Nunca `force`."""
        body = dict(extra)
        body.update(self.config.creds())
        if board.mac:
            body["expect_mac"] = board.mac
        if self.holds_reservation(board):
            body["require_reservation"] = True
        return body

    # ----- escrituras -----

    def send(self, board: Board, text: str, enter: bool = True) -> dict:
        return self.request("POST", f"/api/device/{board.tty}/send", body=self._write_body(board, text=text,
                                                                                           enter=enter))

    def command(self, board: Board, command: str) -> dict:
        return self.request("POST", f"/api/device/{board.tty}/command/{command}", body=self._write_body(board))

    def reserve(self, board: Board, ttl_s: int) -> dict:
        body = {**self.config.require_creds("reserve"), "ttl_s": int(ttl_s)}
        if board.mac:
            body["expect_mac"] = board.mac
        r = self.request("POST", f"/api/device/{board.tty}/reserve", body=body)
        data = self._resv_load()
        data[self._resv_key(board)] = {"user": self.config.lock_user, "expires": r.get("expires"),
                                       "tty": board.tty, "board": board.label}
        self._resv_save(data)
        return r

    def release(self, board: Board) -> dict:
        r = self.request("POST", f"/api/device/{board.tty}/release", body=self.config.require_creds("release"))
        data = self._resv_load()
        if data.pop(self._resv_key(board), None) is not None:
            self._resv_save(data)
        return r

    def restart_session(self, board: Board) -> dict:
        r = self.request("POST", f"/api/device/{board.tty}/devremote-reset", body=self._write_body(board))
        if not (r or {}).get("ok"):
            raise EspbenchError("unexpected", f"devremote --reset falló: {(r or {}).get('stderr') or r}")
        return r

    # ----- rango del log con espera -----

    def read_range(self, key: str, since: Optional[str] = None, until: Optional[str] = None,
                   around: Optional[str] = None, before: Optional[int] = None, after: Optional[int] = None,
                   grep: Optional[str] = None, src: Optional[str] = None, max_lines: Optional[int] = None,
                   timeout_s: Optional[float] = None, for_s: Optional[float] = None, echo: Optional[str] = None,
                   expect_panic: bool = False, idle_needs_output: bool = False,
                   fail_on=CRASH_TYPES) -> dict:
        """Rango del log (§5, §7.3) con la espera del lado del cliente.

        - Sin until/idle/for: un solo pedido (rango histórico).
        - `until` (lo evalúa el server): poll cada poll_s desde el `end` anterior,
          con `echo` hasta que llega `echo_seen`.
        - `until="idle:D"`: sin líneas nuevas por D (con idle_needs_output, recién
          después de la primera: un send espera la respuesta).
        - `for_s`: ventana fija (con until, es el tope: no encontrado = timeout).
        - Un `panic`/`boot_loop` (fail_on) en el rango corta: error `crashed`, o
          ok con expect_panic. Salvo que sea lo que se buscaba (until=panic).
        - `session_ended` sin until_found: error `session_ended`.

        Devuelve un dict con `error` (None, timeout, crashed, session_ended) y
        `reason` (range, until, idle, for, panic). Los errores HTTP levantan
        EspbenchError."""
        params = {"grep": grep, "src": src, "max_lines": max_lines}
        if around:
            r = self.board_log(key, around=around, before=before, after=after, **params)
            return self._result(key, _single(r, max_lines), reason="range", error=None)
        idle_s = None
        server_until = until or None
        if until and until.startswith("idle:"):
            idle_s = parse_duration(until[5:], "idle")
            server_until = None
        waiting = server_until is not None or idle_s is not None or for_s is not None
        if not waiting:
            r = self.board_log(key, since=since, **params)
            return self._result(key, _single(r, max_lines), reason="range", error=None)

        if timeout_s is None:
            timeout_s = DEFAULT_TIMEOUT_S
        fail_on = tuple(t for t in fail_on if t != server_until)
        acc = _Range(max_lines or DEFAULT_MAX_LINES)
        cur = since or "session"
        echo_pending = echo if server_until else None
        known = self._crash_snapshot(key, fail_on) if fail_on else set()
        t0 = self._clock()
        last_change, seen_output, prev_end, active = t0, False, None, False
        while True:
            r = self.board_log(key, since=cur, until=server_until, echo=echo_pending, **params)
            acc.add(r)
            now = self._clock()
            if r.get("echo_seen"):
                echo_pending = None
            changed = r.get("end") != (prev_end if prev_end is not None else r.get("start"))
            if changed:
                last_change, seen_output = now, True
            prev_end = r.get("end")
            # Con actividad (en este poll o el anterior), los crash salen de /events
            # desde el start: ver _crash_since.
            crash = self._crash_since(key, acc, fail_on, known) if fail_on and (changed or active) else None
            active = changed
            if crash is not None:
                return self._finish(key, acc, crash, expect_panic, known=known)
            if r.get("until_found"):
                return self._finish(key, acc, None, expect_panic, "until", None, fail_on, known)
            if r.get("session_ended"):
                return self._finish(key, acc, None, expect_panic, "session_ended", "session_ended", fail_on,
                                    known)
            elapsed = now - t0
            if idle_s is not None and (seen_output or not idle_needs_output) and now - last_change >= idle_s:
                return self._finish(key, acc, None, expect_panic, "idle", None, fail_on, known)
            if for_s is not None and elapsed >= for_s:
                return self._finish(key, acc, None, expect_panic, "for", "timeout" if server_until else None,
                                    fail_on, known)
            if for_s is None and elapsed >= timeout_s:
                return self._finish(key, acc, None, expect_panic, "timeout", "timeout", fail_on, known)
            cur = r.get("end") or cur
            self._sleep(self.poll_s)

    def _crash_events(self, key: str, fail_on) -> list:
        """Los últimos panic/boot_loop (fail_on) de la placa, en orden de log."""
        try:
            return (self.board_events(key, types=",".join(fail_on), limit=50) or {}).get("events") or []
        except EspbenchError as e:
            if e.error == "network":
                raise
            return []

    def _crash_snapshot(self, key: str, fail_on) -> set:
        return {(e.get("type"), e.get("cursor")) for e in self._crash_events(key, fail_on)}

    def _crash_since(self, key: str, acc: _Range, fail_on, known: set) -> Optional[dict]:
        """Primer panic/boot_loop (fail_on) del rango según /events: uno con
        cursor en [start, end), o uno NUEVO (no estaba al empezar la espera) de
        la misma sesión con cursor antes de end.

        Los `events` de cada respuesta de /log no alcanzan: (1) un evento se
        escribe un instante después de su línea, y si llegó tarde a un poll el
        siguiente (desde `end`) ya no lo incluye; (2) el cursor de un evento es
        el inicio de su línea LÓGICA: un panic que llega como `↪` de un prompt de
        esp_console (`esp> ` sin \\n, que ya salió al archivo) queda con el
        cursor del prompt, anterior al start del poll que lo trae (y hasta al
        start del rango)."""
        start, end = parse_cursor(acc.start), parse_cursor(acc.end)
        if not start or not end or start[0] != end[0]:
            return None
        for e in self._crash_events(key, fail_on):
            c = parse_cursor(e.get("cursor"))
            if not c or c[0] != start[0] or c[1] >= end[1]:
                continue
            if c[1] >= start[1] or (e.get("type"), e.get("cursor")) not in known:
                if all((x.get("type"), x.get("cursor")) != (e.get("type"), e.get("cursor")) for x in acc.events):
                    acc.events.append(e)
                return e
        return None

    def _finish(self, key: str, acc: _Range, crash: Optional[dict], expect_panic: bool,
                reason: str = "crash", error: Optional[str] = "crashed", fail_on=(), known=None) -> dict:
        """Cierra una espera. Sin crash visto, un último /events: el del final
        puede haberse escrito después del último poll."""
        if crash is None and fail_on and acc.start and acc.end:
            crash = self._crash_since(key, acc, fail_on, known or set())
        if crash is not None:
            if expect_panic and crash.get("type") == "panic":
                reason, error = "panic", None
            else:
                reason, error = "crash", "crashed"
        out = self._result(key, acc, reason=reason, error=error)
        if crash is not None:
            out["crash"] = crash
        return out

    def _result(self, key: str, acc: _Range, reason: str, error: Optional[str]) -> dict:
        last = acc.last
        lines = acc.lines()
        out = {
            "ok": error is None, "board": key, "reason": reason,
            "date": acc.date, "lines": lines, "start": acc.start, "end": acc.end,
            "until_found": last.get("until_found"), "match": last.get("match"),
            "truncated": acc.truncated, "session_ended": bool(last.get("session_ended")),
            "events": acc.events, "server_time": last.get("server_time"),
        }
        if error is not None:
            out["error"] = error
            out["message"] = _RANGE_MESSAGES.get(error, error)
        return out

    # ----- flash y verificación -----

    def _wait_new_session(self, key: str, old_sid: str, deadline: float) -> Optional[str]:
        """Placas USB-Serial-JTAG (S3/C3): el reset re-enumera el USB y arranca
        una sesión nueva del proceso. Espera a que output.log tenga otra."""
        while self._clock() < deadline:
            try:
                sid = (self.board_events(key, limit=1) or {}).get("session")
            except EspbenchError as e:
                if e.error == "network":
                    raise
                sid = None
            if sid and sid != old_sid:
                return sid
            self._sleep(self.poll_s)
        return None

    def wait_ready(self, board: Board, timeout_s: float = READY_TIMEOUT_S) -> Optional[Board]:
        """Espera state == monitoring (o unknown) de la placa por MAC (el tty
        puede cambiar en una re-enumeración). None si no llegó."""
        deadline = self._clock() + timeout_s
        while True:
            for d in self.devices():
                if board.mac and bare_mac(d.get("mac")) == bare_mac(board.mac) \
                        and d.get("state") in ("monitoring", "unknown"):
                    return Board.from_info(board.name, d)
            if self._clock() >= deadline:
                return None
            self._sleep(self.poll_s)

    def verify(self, board: Board, since: str, window_s: float = VERIFY_WINDOW_S, until: Optional[str] = None,
               timeout_s: float = VERIFY_TIMEOUT_S, expect_panic: bool = False,
               max_lines: Optional[int] = None) -> dict:
        """--verify (§8.2, §12.1): el primer `boot` después de `since` (si la
        sesión termina sin boot, sigue en la sesión nueva), y después una ventana
        de asentamiento: falla si en ella hay otro boot, un panic o boot_loop.
        Con `until`, además espera X desde el boot."""
        key = board.key
        deadline = self._clock() + timeout_s
        cur, sid = since, (parse_cursor(since) or (None,))[0]
        sessions = []
        while True:
            remaining = max(0.0, deadline - self._clock())
            r = self.read_range(key, since=cur, until="boot", timeout_s=remaining, max_lines=max_lines,
                                expect_panic=expect_panic)
            if r.get("error") == "session_ended" and not r.get("until_found") and sid:
                new = self._wait_new_session(key, sid, deadline)
                if new is None:
                    r["message"] = "la sesión terminó y no arrancó otra a tiempo"
                    return self._verify_result(r, None, None, window_s, sessions)
                self.log(f"sesión nueva {new} (la placa re-enumeró el USB): sigo ahí")
                sessions.append(new)
                cur, sid = f"c:{new}:0", new
                continue
            break
        if r.get("error") or r.get("reason") == "panic":
            if r.get("error") == "timeout":
                r["message"] = "no apareció el boot después del flash/reset"
            return self._verify_result(r, None, None, window_s, sessions)
        boot_end = r["end"]
        w = self.read_range(key, since=boot_end, for_s=window_s, max_lines=max_lines, expect_panic=expect_panic,
                            fail_on=("panic", "boot_loop", "boot")) if window_s > 0 else None
        if w is not None and w.get("error") == "crashed" and (w.get("crash") or {}).get("type") == "boot":
            w["message"] = "la placa se reinició en la ventana de asentamiento"
        u = None
        if until and (w is None or w.get("ok")) and (w is None or w.get("reason") != "panic"):
            remaining = max(1.0, deadline - self._clock())
            u = self.read_range(key, since=boot_end, until=until, timeout_s=remaining, max_lines=max_lines,
                                expect_panic=expect_panic)
        return self._verify_result(r, w, u, window_s, sessions)

    def _verify_result(self, boot: dict, window: Optional[dict], until: Optional[dict], window_s: float,
                       sessions: list) -> dict:
        failed = next((x for x in (boot, window, until) if x is not None and x.get("error")), None)
        later = until if until is not None and window is not None and _cursor_lt(window.get("end"),
                                                                                until.get("end")) else window
        if later is None:
            later = until
        lines = list(boot.get("lines") or []) + list((later or {}).get("lines") or [])
        evs = list(boot.get("events") or []) + list((later or {}).get("events") or [])
        out = {
            "ok": failed is None,
            "boot": boot.get("match") if boot.get("until_found") else None,
            "boot_cursor": boot.get("end") if boot.get("until_found") else None,
            "window_s": window_s,
            "new_session": sessions[-1] if sessions else None,
            "lines": lines, "events": evs,
            "end": (later or boot).get("end"),
            "reason": (failed or later or boot).get("reason"),
        }
        if until is not None:
            out["match"] = until.get("match")
        crash = next((x.get("crash") for x in (boot, window, until) if x is not None and x.get("crash")), None)
        if crash is not None:
            out["crash"] = crash
        if failed is not None:
            out["error"], out["message"] = failed["error"], failed.get("message")
        return out

    def flash(self, board: Board, build_dir: pathlib.Path, chip: str = "auto", baud: int = 921600,
              encrypt: bool = True, erase: bool = False, on_line: Optional[Callable[[str], None]] = None) -> dict:
        """Flash por el protocolo TCP. Devuelve la respuesta compacta (con el
        `cursor` del evento flash); los errores levantan EspbenchError."""
        creds = self.config.require_creds("flash")
        if not board.live or not board.port_tcp:
            raise EspbenchError("not_found", f"la placa '{board.name}' no está conectada")
        if self.holds_reservation(board) and not (board.lock_user == self.config.lock_user and board.lock_expires):
            raise EspbenchError("reservation_lost", f"la reserva de '{board.label}' ya no es tuya "
                                                    f"(lock: {board.lock_user or 'nadie'})")
        build_dir = pathlib.Path(build_dir)
        artifact = collect_artifact(build_dir, include_elf=True, log=self.log)
        warnings = []
        model = hw_model_from_build(build_dir)
        if model and board.hw_model and model != board.hw_model:
            warnings.append(f"hw_model distinto: artifact={model} placa={board.hw_model}")
        try:
            remote = {"name": board.label, "host": self.config.hostname, "port": board.port_tcp,
                      "token": self.config.token, **creds}
            resp = flash_one(remote, artifact, sha256_file(artifact), artifact.stat().st_size,
                             time.strftime("job_%Y%m%d_%H%M%S"), chip, baud, encrypt, erase,
                             on_status=self.log, on_line=on_line)
        finally:
            shutil.rmtree(artifact.parent, ignore_errors=True)
        out = {k: resp.get(k) for k in ("job_id", "status", "cursor", "erase_rc", "write_rc", "missing_app",
                                        "error_hint") if resp.get(k) is not None}
        if warnings:
            out["warnings"] = warnings
        if resp.get("ok"):
            return {"ok": True, **out}
        out["log_tail"] = [l for l in resp.get("logs") or [] if l.strip()][-20:]
        if resp.get("network"):
            raise EspbenchError("network", f"flash: sin conexión con {remote['host']}:{remote['port']}: "
                                           f"{resp.get('error')}", data=out)
        code = resp.get("code") or resp.get("error")
        error = _FLASH_ERRORS.get(code, "flash_failed")
        message = resp.get("message") or resp.get("error_hint") or str(resp.get("error") or "flash fallido")
        raise EspbenchError(error, message, data=out)


_RANGE_MESSAGES = {
    "timeout": "no apareció el until antes del timeout",
    "crashed": "la placa crasheó (panic / boot loop) en el rango",
    "session_ended": "la placa se desconectó o el proceso se relanzó durante la espera",
}


def _single(r: dict, max_lines: Optional[int]) -> _Range:
    acc = _Range(max(max_lines or DEFAULT_MAX_LINES, len(r.get("lines") or [])))
    acc.add(r)
    return acc


def _http_error(e: urllib.error.HTTPError) -> EspbenchError:
    try:
        body = json.loads(e.read() or b"null")
    except (ValueError, OSError):
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and detail.get("error"):
        return EspbenchError(str(detail["error"]), str(detail.get("message") or detail["error"]), e.code)
    message = detail if isinstance(detail, str) else f"HTTP {e.code}"
    if not isinstance(detail, (str, dict)) and detail is not None:
        message = json.dumps(detail)[:300]
    return EspbenchError(_HTTP_ERRORS.get(e.code, "unexpected"), message, e.code)


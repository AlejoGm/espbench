"""
api.py — backend del dashboard: FastAPI con la API REST, el WebSocket de logs
y los archivos estáticos de remote/dashboard/.

Corre como servicio aparte (dashboard.service, `uvicorn server.api:app`), no
dentro de los procesos de los devices: todo lo que sabe de ellos lo lee de
disco (DeviceRegistry: run/<tty>.json + devices.json; LogStreamer: el log que
publica cada device). Antes se llamaba dashboard.py y chocaba de nombre con
remote/dashboard/, que es el frontend.
"""
import asyncio
import dataclasses
import pathlib
import re
import subprocess
import time
from typing import Any, Optional
from urllib.parse import unquote

from fastapi import Body, FastAPI, Header, HTTPException, WebSocket
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from server import auth, events, history, locks, logrange, paths, runstate, taglog
from server.device_registry import DeviceRegistry, DevicesFile
from server.log_streamer import LogStreamer

TAG = "api"
BASE_DIR = pathlib.Path(__file__).parent.parent
DASHBOARD_DIR = BASE_DIR / "dashboard"

app = FastAPI()
registry = DeviceRegistry()
streamer = LogStreamer(registry=registry)


@app.on_event("startup")
async def _startup():
    streamer.scan_all()


@app.get("/api/version")
async def get_version():
    try:
        version_file = paths.version_file()
        version = version_file.read_text().strip() if version_file.exists() else "dev"
    except Exception:
        version = "dev"
    return {"version": version}


@app.get("/api/devices")
async def get_devices():
    return [dataclasses.asdict(d) for d in registry.list_devices()]


@app.get("/api/device/by-key/{device_key:path}")
async def get_device_by_key(device_key: str):
    device = registry.get_device_by_key(device_key)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device con key '{device_key}' no encontrado")
    return dataclasses.asdict(device)


# ---------- historial y consola ----------
# Van antes de /api/device/{tty:path}: ese path se come todo lo que sigue.

_TTY_RE = re.compile(r"(ttyUSB|esp-slot)\d+")
SEND_MAX = 256
BUSY_STATES = ("flashing", "erasing")
RESERVE_DEFAULT_S = 1800
RESERVE_MAX_S = 7 * 24 * 3600


def _fail(status: int, error: str, message: str):
    """Error con `error` estable (el contrato del CLI, docs/specs/agents-cli.md
    §8.3) y un mensaje para humanos: {"detail": {"error", "message"}}."""
    raise HTTPException(status_code=status, detail={"error": error, "message": message})


def _require_auth(authorization) -> None:
    """Escrituras: si hay /opt/esp/api_token, `Authorization: Bearer <token>`.
    Llamado directo (tests), el default de Header() no es un str: cuenta como
    ausente."""
    try:
        ok = auth.bearer_ok(authorization if isinstance(authorization, str) else None)
    except auth.AuthConfigError as e:
        taglog.error(TAG, f"token de la API ilegible, escrituras rechazadas: {e}")
        _fail(500, "auth_config", "el token de la API de la Pi no se puede leer (ver /opt/esp/api_token)")
    if not ok:
        _fail(401, "auth", "falta el token de la API (Authorization: Bearer <token>) o es incorrecto")


def _check_tty(tty: str) -> None:
    if not _TTY_RE.fullmatch(tty):
        _fail(400, "bad_request", f"tty no válido: {tty}")


def _creds(body: dict, required: bool = True):
    user = str(body.get("lock_user") or "").strip()
    token = str(body.get("lock_token") or "").strip()
    if required and (not user or not token):
        _fail(400, "bad_request", "lock_user y lock_token requeridos")
    if any(v and not locks.valid_credential(v) for v in (user, token)):
        _fail(400, "bad_request", "lock_user y lock_token no pueden tener ':'")
    return user, token


def _check_expect_mac(state: dict, expect_mac) -> None:
    """La escritura es para una placa (por MAC) y el tty puede tener otra (replug,
    renumeración): 409 device_changed."""
    if not expect_mac:
        return
    if locks.normalize_mac(state.get("mac")) != locks.normalize_mac(str(expect_mac)):
        _fail(409, "device_changed", f"en este tty está {state.get('mac') or 'una placa sin MAC'}, "
                                     f"no {expect_mac}")


def _forced(body: dict) -> bool:
    """Solo el booleano true fuerza ("false", 1 o "yes" no). El CLI nunca lo manda."""
    return body.get("force") is True


def _check_reservation(tty: str, body: dict) -> bool:
    """A3: una reserva (lock con vencimiento) bloquea send/command de otros. El
    lock permanente del flash no (si no, la consola del dashboard muere en toda
    placa flasheada). `force: true` la saltea (el dashboard, después de
    confirmar). `require_reservation: true` (el CLI): la escritura solo sale si
    el par tiene la reserva vigente, en el mismo pedido (sin carrera entre
    chequear y escribir) → 423 reservation_lost. Devuelve True si forzó."""
    lock = locks.read(tty)
    user, token = _creds(body, required=False)
    if body.get("require_reservation") is True:
        if lock is None or not lock.reservation or not lock.owned_by(user, token):
            who = f"'{lock.user}'" if lock is not None else "nadie"
            _fail(423, "reservation_lost", f"la reserva ya no es tuya (la tiene {who})")
        return False
    if lock is None or not lock.reservation or lock.owned_by(user, token):
        return False
    if _forced(body):
        return True
    _fail(423, "locked", f"reservada por '{lock.user}' hasta {lock.expires_iso()}")


def _record(state: dict, type_: str, detail: dict, cursor: Optional[str] = None) -> Optional[str]:
    """Evento del api en el events.jsonl de la placa que está en el tty. Que no se
    pueda registrar no rompe la escritura."""
    log_path = state.get("log_path")
    if not log_path:
        return None
    try:
        if cursor is None:
            ev = events.record(log_path, type_, detail)
            return ev["cursor"] if ev else None
        events.append(paths.events_file_beside(log_path), events.make(type_, cursor, detail, by="api"))
        return cursor
    except OSError:
        return None


# ---------- lecturas por placa (device_key, SN o MAC) ----------
# Funcionan con la placa desconectada: los datos viven en devices/<MAC>/. La
# sesión "actual" de una placa desconectada es la última (su output.log).
# Handlers sync: leen archivos, FastAPI los corre en un threadpool.

_RANGE_STATUS = {"bad_anchor": 400, "bad_request": 400, "cursor_expired": 410, "not_found": 404}


def _board_mac(key: str) -> str:
    mac = DevicesFile().resolve_board(key)
    if mac is None or not paths.device_home(mac).is_dir():
        _fail(404, "not_found", f"no hay placa '{key}' (device_key, SN o MAC)")
    return mac


def _board_live(mac: str) -> bool:
    """Hay un proceso vivo escribiendo el output.log de esa placa."""
    log = str(paths.device_output_log(mac))
    # run/<tty>.json queda en "disconnected" a propósito (esp32_tmux.sh): si la
    # placa volvió en otro tty hay dos con el mismo log_path.
    return any(state.get("log_path") == log and state.get("state") != "disconnected"
               and runstate.pid_alive(state.get("pid"))
               for state in runstate.list_all().values())


def _range_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except logrange.RangeError as e:
        _fail(_RANGE_STATUS.get(e.error, 400), e.error, e.message)


@app.get("/api/board/{key}/log")
def board_log(key: str, since: Optional[str] = None, until: Optional[str] = None,
              around: Optional[str] = None, before: Optional[str] = None, after: Optional[str] = None,
              max_lines: Optional[str] = None, grep: Optional[str] = None, src: Optional[str] = None,
              raw: bool = False, echo: Optional[str] = None):
    """Rango del log (docs/specs/agents-cli.md §5 y §7.3)."""
    mac = _board_mac(key)
    return _range_call(logrange.read_range, paths.device_home(mac), since=since, until=until, around=around,
                       before=before, after=after, max_lines=max_lines, grep=grep, src=src, raw=raw,
                       echo=echo, live=_board_live(mac))


@app.get("/api/board/{key}/events")
def board_events(key: str, type: Optional[str] = None, since: Optional[str] = None,
                 limit: Optional[str] = None, order: Optional[str] = None):
    """Eventos de la placa (events.jsonl), por (sesión, offset): los últimos
    `limit`, o los primeros desde since con order=asc; `more` si quedaron más."""
    mac = _board_mac(key)
    return _range_call(logrange.list_events, paths.device_home(mac), types=type, since=since, limit=limit,
                       order=order)


@app.get("/api/device/{tty}/jobs")
async def device_jobs(tty: str, limit: int = 30):
    _check_tty(tty)
    return history.list_jobs(tty, registry._get_tty_mac(tty), limit=max(1, min(limit, 200)))


@app.get("/api/device/{tty}/jobs/{job_id}/log", response_class=PlainTextResponse)
async def device_job_log(tty: str, job_id: str):
    _check_tty(tty)
    text = history.job_log(tty, registry._get_tty_mac(tty), job_id)
    if text is None:
        raise HTTPException(status_code=404, detail=f"job '{job_id}' sin log")
    return text


@app.get("/api/device/{tty}/sessions")
async def device_sessions(tty: str):
    _check_tty(tty)
    return history.list_sessions(tty, registry._get_tty_mac(tty))


@app.get("/api/device/{tty}/sessions/{name}")
async def device_session(tty: str, name: str, download: bool = False):
    _check_tty(tty)
    mac = registry._get_tty_mac(tty)
    path = history.session_path(tty, mac, name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"sesión '{name}' no encontrada")
    if download:
        return FileResponse(path, media_type="text/plain", filename=f"{tty}_{name}")
    return PlainTextResponse(history.read_session(tty, mac, name) or "")


def send_keys_cmds(session: str, text: str, enter: bool) -> list:
    """Comandos tmux para escribir `text` en la consola del monitor. `-l`: literal,
    así "C-r" o "Enter" dentro del texto no se interpretan como teclas."""
    cmds = []
    if text:
        cmds.append(["tmux", "send-keys", "-t", session, "-l", text])
    if enter:
        cmds.append(["tmux", "send-keys", "-t", session, "Enter"])
    return cmds


@app.post("/api/device/{tty}/send")
async def device_send(tty: str, body: dict = Body(...), authorization: Optional[str] = Header(None)):
    """Manda texto por el serial del device (a través del monitor en tmux).
    Devuelve el cursor del log previo al envío (desde ahí se busca la
    respuesta) y lo registra como evento `send`."""
    _require_auth(authorization)
    _check_tty(tty)
    text = str(body.get("text", ""))
    enter = bool(body.get("enter", True))
    if len(text) > SEND_MAX:
        _fail(400, "bad_request", f"texto de más de {SEND_MAX} caracteres")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in text):
        _fail(400, "bad_request", "caracteres de control no permitidos")
    if not text and not enter:
        _fail(400, "bad_request", "nada para mandar")
    user, _ = _creds(body, required=False)
    state = runstate.read(tty) or {}
    _check_expect_mac(state, body.get("expect_mac"))
    if state.get("state") in BUSY_STATES:
        _fail(409, "busy", f"device ocupado ({state['state']})")
    forced = _check_reservation(tty, body)
    cursor = events.log_end_cursor(state["log_path"]) if state.get("log_path") else None
    session = f"esp32_{tty}"
    for cmd in send_keys_cmds(session, text, enter):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            _fail(502, "unexpected", "tmux no disponible")
        if r.returncode != 0:
            _fail(502, "unexpected", f"tmux: {r.stderr.strip() or r.returncode}")
    if cursor is not None:
        detail = {"text": text, "enter": enter, "user": user or None}
        if forced:
            detail["forced"] = True
        _record(state, "send", detail, cursor)
    return {"ok": True, "session": session, "sent": text, "enter": enter, "cursor": cursor}


@app.post("/api/device/{tty}/reserve")
async def device_reserve(tty: str, body: dict = Body(...), authorization: Optional[str] = Header(None)):
    """Reserva con vencimiento (docs/specs/agents-cli.md §6), con el mismo par
    lock_user/lock_token que el flash. Volver a reservar renueva el vencimiento."""
    _require_auth(authorization)
    _check_tty(tty)
    user, token = _creds(body)
    try:
        ttl = int(RESERVE_DEFAULT_S if body.get("ttl_s") is None else body["ttl_s"])
    except (TypeError, ValueError):
        _fail(400, "bad_request", "ttl_s tiene que ser un entero")
    if not 1 <= ttl <= RESERVE_MAX_S:
        _fail(400, "bad_request", f"ttl_s entre 1 y {RESERVE_MAX_S}")
    state = runstate.read(tty) or {}
    _check_expect_mac(state, body.get("expect_mac"))
    if not state.get("mac"):
        # Sin MAC la reserva no puede atarse a la placa (replug/renumeración)
        _fail(409, "busy", f"la placa de {tty} todavía no tiene MAC ({state.get('state') or 'sin proceso'}): "
                           "reintentar cuando esté en monitoring")
    with locks.exclusive(tty):
        lock = locks.read(tty)
        if lock is not None and lock.user != user:
            if lock.reservation:
                _fail(409, "locked", f"la tiene '{lock.user}' hasta {lock.expires_iso()}")
            _fail(409, "locked", f"lock permanente de '{lock.user}' (de su último flash): que lo suelte "
                                 f"con unlock/release, o `devremote --unlock {tty}` en la Pi")
        if lock is not None and lock.token != token:
            _fail(403, "token_mismatch", "par user/token incorrecto")
        new = locks.Lock(user, token, int(time.time()) + ttl, locks.normalize_mac(state.get("mac")))
        locks.write(tty, new)
    _record(state, "reserve", {"user": user, "expires": new.expires_iso()})
    return {"ok": True, "tty": tty, "user": user, "expires": new.expires_iso(), "mac": state.get("mac")}


@app.post("/api/device/{tty}/release")
async def device_release(tty: str, body: dict = Body(...), authorization: Optional[str] = Header(None)):
    """Suelta el lock (reserva o el del flash) con el mismo par, como unlock."""
    _require_auth(authorization)
    _check_tty(tty)
    user, token = _creds(body)
    lock = _drop_lock(tty, user, token)
    if lock is None:
        return {"ok": True, "message": "no estaba bloqueado"}
    _record(runstate.read(tty) or {}, "release", {"user": user, "expires": lock.expires_iso()})
    return {"ok": True, "message": "liberado"}


def _drop_lock(tty: str, user: str, token: str) -> Optional[locks.Lock]:
    """Borra el lock vigente si es del par (403 si no). None si no había."""
    with locks.exclusive(tty):
        lock = locks.read(tty)          # uno vencido no existe
        if lock is None:
            return None
        if not lock.owned_by(user, token):
            _fail(403, "token_mismatch", "par user/token incorrecto")
        locks.remove(tty)
        return lock


@app.get("/api/device/{tty:path}")
async def get_device(tty: str):
    device = registry.get_device(tty)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{tty}' not found")
    return dataclasses.asdict(device)


_devices_file = DevicesFile()


@app.patch("/api/devices/{mac:path}")
async def patch_device(mac: str, body: dict = Body(...), authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    device_key = str(body.get("device_key") or "").strip()
    if not device_key:
        _fail(400, "bad_request", "device_key requerido")
    bare = unquote(mac).upper().replace("-", "").replace(":", "")
    if not re.fullmatch(r"[0-9A-F]{12}", bare):
        _fail(400, "bad_request", f"MAC inválida: {mac}")
    mac_norm = ":".join(bare[i:i + 2] for i in range(0, 12, 2))
    try:
        _devices_file.update_device_key(mac_norm, device_key)
    except Exception as e:
        _fail(500, "unexpected", str(e))
    return {"ok": True}


@app.post("/api/device/{tty}/unlock")
async def device_unlock(tty: str, body: dict = Body(...), authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    _check_tty(tty)
    user, token = _creds(body)
    if _drop_lock(tty, user, token) is None:
        return {"ok": True, "message": "no estaba bloqueado"}
    return {"ok": True, "message": "desbloqueado"}


_COMMANDS = {
    "reset":      ["C-t", "C-r"],  # Ctrl+T Ctrl+R — reset via RTS
    "bootloader": ["C-t", "C-p"],  # Ctrl+T Ctrl+P — reset into bootloader
}

@app.post("/api/device/{tty}/command/{command}")
async def device_command(tty: str, command: str, body: Optional[dict] = Body(None),
                         authorization: Optional[str] = Header(None)):
    """Teclas al monitor (reset / bootloader). Body opcional: expect_mac,
    lock_user/lock_token (dueño de la reserva), force."""
    _require_auth(authorization)
    _check_tty(tty)
    if command not in _COMMANDS:
        _fail(400, "bad_request", f"Comando desconocido: {command}")
    body = body if isinstance(body, dict) else {}
    user, _ = _creds(body, required=False)
    state = runstate.read(tty) or {}
    _check_expect_mac(state, body.get("expect_mac"))
    if state.get("state") in BUSY_STATES:
        _fail(409, "busy", f"device ocupado ({state['state']})")
    forced = _check_reservation(tty, body)
    session = f"esp32_{tty}"
    for key in _COMMANDS[command]:
        try:
            r = subprocess.run(["tmux", "send-keys", "-t", session, key], capture_output=True, text=True)
        except FileNotFoundError:
            _fail(502, "unexpected", "tmux no disponible")
        if r.returncode != 0:
            _fail(502, "unexpected", f"tmux: {r.stderr.strip() or r.returncode}")
        await asyncio.sleep(0.05)
    detail = {"command": command, "user": user or None}
    if forced:
        detail["forced"] = True
    cursor = _record(state, "command", detail)
    return {"ok": True, "command": command, "session": session, "cursor": cursor}


@app.post("/api/device/{tty}/devremote-reset")
async def devremote_reset(tty: str, authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    _check_tty(tty)   # ttyUSBN o esp-slotK: devremote resuelve el nombre.
    result = subprocess.run(
        ["/usr/local/bin/devremote", "--reset", tty],
        capture_output=True, text=True
    )
    return {"ok": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr}


@app.websocket("/ws/device/{tty:path}")
async def ws_device(websocket: WebSocket, tty: str):
    await streamer.subscribe(tty, websocket)


class _RevalidatedStaticFiles(StaticFiles):
    """Estáticos con `Cache-Control: no-cache`: el navegador revalida (ETag, 304)
    en cada carga. Sin esto, después de un update seguía usando el JS/CSS viejo."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", _RevalidatedStaticFiles(directory=str(DASHBOARD_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

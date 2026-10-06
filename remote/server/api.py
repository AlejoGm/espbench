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
import socket
import subprocess
import time
from typing import Any, Optional
from urllib.parse import unquote

from fastapi import Body, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from server import auth, board_meta, events, history, locks, logrange, paths, runstate, taglog
from server import update as bench_update
from server.device_registry import DeviceRegistry, DevicesFile, DevicesFileCorrupt
from server.log_streamer import LogStreamer

TAG = "api"
BASE_DIR = pathlib.Path(__file__).parent.parent
DASHBOARD_DIR = BASE_DIR / "dashboard"

app = FastAPI()
registry = DeviceRegistry()
streamer = LogStreamer()


def _read_first_line(path: pathlib.Path) -> str:
    try:
        return path.read_text().strip() if path.exists() else ""
    except OSError:
        return ""


@app.get("/api/version")
async def get_version():
    """También es la identidad del bench para bench-master: `app` dice que es un
    espbench (lo distingue de otros hosts de la tailnet) y `name`, cómo se llama.
    `auth`: la Pi tiene token de la API (el dashboard muestra "Forzar" solo si lo
    hay: `unlock` forzado lo exige)."""
    try:
        has_token = bool(auth.read_token())
    except auth.AuthConfigError:
        has_token = True        # falla cerrado: hay archivo, ilegible
    return {
        "app": "espbench",
        "version": _read_first_line(paths.version_file()) or "dev",
        "name": _read_first_line(paths.bench_name_file()) or socket.gethostname(),
        "auth": has_token,
    }


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
# Tope de una reserva: 24 h. Más es un banco bloqueado por un agente que se
# olvidó de soltarla (y sin api_token nadie la puede forzar). Pasarse → 400
# (sin clamp: que el pedido no reserve algo distinto de lo que se pidió).
RESERVE_MAX_S = 24 * 3600


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
        _fail(400, "bad_request", "lock_user y lock_token no pueden tener saltos de línea")
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


def _forced_by(body: dict, request) -> dict:
    """Quién forzó, para el evento: el lock_user del pedido (si vino) y el host.
    Llamado directo (tests, benchsim) no hay request."""
    client = getattr(request, "client", None)
    return {"forced": True, "by_user": str(body.get("lock_user") or "").strip() or None,
            "by_host": getattr(client, "host", None)}


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
            _fail(423, "reservation_lost", _lost_message(lock, user))
        return False
    if lock is None or not lock.reservation or lock.owned_by(user, token):
        return False
    if _forced(body):
        return True
    _fail(423, "locked", f"reservada por '{lock.user}' hasta {lock.expires_iso()}")


def _lost_message(lock: Optional[locks.Lock], user: str) -> str:
    if lock is None:
        return "la reserva venció o la soltaron (no hay lock)"     # el CLI agrega cómo volver a reservar
    if not lock.reservation:
        return f"no hay reserva: hay un lock de flash de '{lock.user}' (sin vencimiento)"
    if lock.user == user:
        return f"la reserva es de '{lock.user}' pero con otro lock_token (hasta {lock.expires_iso()})"
    return f"la reserva ya no es tuya: la tiene '{lock.user}' hasta {lock.expires_iso()}"


def _tmux_failed(tty: str, stderr: str):
    """tmux sin la sesión del device: el proceso de la placa no corre (o se
    está relanzando). session_down, no un error interno."""
    _fail(502, "session_down", f"la sesión esp32_{tty} no responde ({stderr}): el proceso de la placa no "
                               "corre; `espbench restart-session` o esperar a que esp32_tmux.sh la relance")


async def _run(cmd: list):
    """subprocess.run en un thread: un handler async que lo llama directo frena
    el event loop entero (los demás pedidos, el WebSocket del vivo) mientras dura
    (`devremote --reset` tarda segundos)."""
    return await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)


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
                 limit: Optional[str] = None, order: Optional[str] = None, counts: bool = False):
    """Eventos de la placa (events.jsonl), por (sesión, offset): los últimos
    `limit`, o los primeros desde since con order=asc; `more` si quedaron más.
    counts=1: además el conteo por tipo de todos (para los chips del dashboard)."""
    mac = _board_mac(key)
    return _range_call(logrange.list_events, paths.device_home(mac), types=type, since=since, limit=limit,
                       order=order, counts=counts)


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
async def device_send(tty: str, body: dict = Body(...), authorization: Optional[str] = Header(None),
                      request: Request = None):
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
            r = await _run(cmd)
        except FileNotFoundError:
            _fail(502, "unexpected", "tmux no disponible")
        if r.returncode != 0:
            _tmux_failed(tty, r.stderr.strip() or str(r.returncode))
    if cursor is not None:
        detail = {"text": text, "enter": enter, "user": user or None}
        if forced:
            detail.update(_forced_by(body, request))
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
        _fail(400, "bad_request", f"ttl_s entre 1 y {RESERVE_MAX_S} (24 h): para más, renová la reserva")
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
    # Con la zona de la Pi (como lock_expires de /api/devices): el que lo lee puede estar en otra
    _record(state, "reserve", {"user": user, "expires": new.expires_iso_tz()})
    return {"ok": True, "tty": tty, "user": user, "expires": new.expires_iso_tz(), "mac": state.get("mac")}


@app.post("/api/device/{tty}/release")
async def device_release(tty: str, body: dict = Body(...), authorization: Optional[str] = Header(None)):
    """Suelta el lock (reserva o el del flash) con el mismo par, como unlock."""
    _require_auth(authorization)
    _check_tty(tty)
    user, token = _creds(body)
    lock = _drop_lock(tty, user, token)
    if lock is None:
        return {"ok": True, "message": "no estaba bloqueado"}
    _record(runstate.read(tty) or {}, "release", {"user": user, "expires": lock.expires_iso_tz()})
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


@app.get("/api/properties")
async def get_properties():
    """Propiedades de las placas: categorías fijas y los valores de este bench."""
    return {"categories": board_meta.catalog()}


@app.post("/api/properties/{cat}/values")
async def add_property_value(cat: str, body: dict = Body(...), authorization: Optional[str] = Header(None)):
    """Agrega un valor a una categoría existente: {id, label?, desc?, warn?, exclude_pick?}
    (warn / exclude_pick solo en `estado`)."""
    _require_auth(authorization)
    try:
        entry = board_meta.add_value(cat, body.get("id"), label=body.get("label"), desc=body.get("desc"),
                                     warn=body.get("warn") is True, exclude_pick=body.get("exclude_pick") is True)
    except board_meta.MetaError as e:
        _fail(400, "bad_request", str(e))
    taglog.info(TAG, f"propiedad nueva: {cat}={entry['id']}")
    return {"ok": True, "category": cat, "value": entry}


@app.delete("/api/properties/{cat}/values/{value}")
async def delete_property_value(cat: str, value: str, authorization: Optional[str] = Header(None)):
    """Borra un valor si ninguna placa lo usa (409 in_use con cuáles)."""
    _require_auth(authorization)
    try:
        # Con el flock de devices.json: un PATCH que lo pone en el medio espera, y después
        # lo valida contra el catálogo ya sin el valor (set_meta valida con el mismo lock).
        _devices_file.locked(lambda data: board_meta.remove_value(
            cat, value, lambda c, v: _devices_file.props_in_use(c, v, data)))
    except DevicesFileCorrupt as e:
        _fail(500, "unexpected", str(e))
    except board_meta.MetaError as e:
        _fail(400, "bad_request", str(e))
    except board_meta.NotFoundError as e:
        _fail(404, "not_found", str(e))
    except board_meta.InUseError as e:
        _fail(409, "in_use", str(e))
    taglog.info(TAG, f"propiedad borrada: {cat}={value}")
    return {"ok": True}


@app.patch("/api/devices/{mac:path}")
async def patch_device(mac: str, body: dict = Body(...), authorization: Optional[str] = Header(None),
                       request: Request = None):
    """Datos de la placa en devices.json, por MAC. Cualquier combinación de:
    - `device_key`: renombrar.
    - `note`: texto corto (aviso, no lock); "" o null la borra. Evento `note`.
    - `props` {cat: valor | [valores] | null}, `props_add` / `props_remove` {cat: valor(es)}:
      valores del catálogo (GET /api/properties); quitar vale para cualquiera. Evento `props`.
    `user`: quién (va en note_by y en los eventos); sin él, `<via>@<host>` (el dashboard
    manda via "dashboard") o el host del pedido."""
    _require_auth(authorization)
    bare = unquote(mac).upper().replace("-", "").replace(":", "")
    if not re.fullmatch(r"[0-9A-F]{12}", bare):
        _fail(400, "bad_request", f"MAC inválida: {mac}")
    mac_norm = ":".join(bare[i:i + 2] for i in range(0, 12, 2))
    wants_key = "device_key" in body
    wants_note = "note" in body
    wants_props = any(k in body for k in ("props", "props_add", "props_remove"))
    if not (wants_key or wants_note or wants_props):
        _fail(400, "bad_request", "nada para cambiar: device_key, note, props, props_add o props_remove")
    device_key = str(body.get("device_key") or "").strip()
    if wants_key and not device_key:
        _fail(400, "bad_request", "device_key requerido")
    try:
        note = board_meta.clean_note(body.get("note")) if wants_note else None
        user = board_meta.clean_user(body.get("user"))
        board_meta.plan_props(body.get("props"), body.get("props_add"), body.get("props_remove"))   # antes de renombrar
    except board_meta.MetaError as e:
        _fail(400, "bad_request", str(e))
    if user is None:       # sin user: quién lo mandó ("dashboard@10.0.0.9") o solo el host
        host = getattr(getattr(request, "client", None), "host", None)
        via = str(body.get("via") or "")
        user = f"{via}@{host}" if re.fullmatch(r"[a-z][a-z0-9-]{0,15}", via) and host else host
    if wants_key:
        try:
            _devices_file.update_device_key(mac_norm, device_key)
        except Exception as e:
            _fail(500, "unexpected", str(e))
    out = {"ok": True}
    if wants_note or wants_props:
        try:
            r = _devices_file.set_meta(mac_norm, user, note=note, props=body.get("props"),
                                       props_add=body.get("props_add"), props_remove=body.get("props_remove"))
        except KeyError:
            _fail(404, "not_found", f"no hay placa {mac_norm} en devices.json")
        except board_meta.MetaError as e:
            _fail(400, "bad_request", str(e))
        except DevicesFileCorrupt as e:
            _fail(500, "unexpected", f"{e} (no se escribió nada: revisarlo en la Pi)")
        except OSError as e:
            _fail(500, "unexpected", f"no se pudo escribir devices.json: {e}")
        entry = r["entry"]
        log = str(paths.device_output_log(mac_norm))
        try:
            if r["note_changed"]:
                events.record(log, "note", {"text": note, "user": user})
            if r["props_changes"]:
                events.record(log, "props", {"changes": r["props_changes"], "user": user})
        except OSError:
            pass        # que no se pueda registrar no rompe la escritura
        out.update(note=entry.get("note"), note_by=entry.get("note_by"), note_at=entry.get("note_at"),
                   props=dict(entry.get("props") or {}))
    return out


@app.post("/api/device/{tty}/unlock")
async def device_unlock(tty: str, body: dict = Body(...), authorization: Optional[str] = Header(None),
                        request: Request = None):
    """Suelta el lock con el par, como release. `force: true` (solo el booleano;
    el dashboard, después de confirmar) lo suelta sin el par y **exige** que la
    Pi tenga /opt/esp/api_token (y el Bearer): sin token cualquiera en la red
    robaría una reserva → 403 force_disabled. Queda un evento `release` con el
    dueño anterior (`user`) y, si se forzó, quién (`by_user`, `by_host`)."""
    _require_auth(authorization)
    _check_tty(tty)
    forced = _forced(body)
    if forced:
        try:
            has_token = bool(auth.read_token())
        except auth.AuthConfigError:
            has_token = True        # _require_auth ya habría fallado
        if not has_token:
            _fail(403, "force_disabled", "forzar requiere /opt/esp/api_token (sin token de la API no se fuerza "
                                         "un unlock): que el dueño la suelte, o `devremote --unlock` en la Pi")
        with locks.exclusive(tty):
            lock = locks.read(tty)
            if lock is not None:
                locks.remove(tty)
    else:
        user, token = _creds(body)
        lock = _drop_lock(tty, user, token)
    if lock is None:
        return {"ok": True, "message": "no estaba bloqueado"}
    detail = {"user": lock.user, "expires": lock.expires_iso_tz()}
    if forced:
        detail.update(_forced_by(body, request))
    _record(runstate.read(tty) or {}, "release", detail)
    return {"ok": True, "message": "desbloqueado", "user": lock.user, "forced": forced}


_COMMANDS = {
    "reset":      ["C-t", "C-r"],  # Ctrl+T Ctrl+R — reset via RTS
    "bootloader": ["C-t", "C-p"],  # Ctrl+T Ctrl+P — reset into bootloader
}

@app.post("/api/device/{tty}/command/{command}")
async def device_command(tty: str, command: str, body: Optional[dict] = Body(None),
                         authorization: Optional[str] = Header(None), request: Request = None):
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
    # Cursor ANTES de las teclas, como en send: el rst: de un reset sale en ms y
    # quedaría antes del cursor (reset --verify, que busca el boot desde ahí, no lo vería).
    cursor = events.log_end_cursor(state["log_path"]) if state.get("log_path") else None
    session = f"esp32_{tty}"
    for key in _COMMANDS[command]:
        try:
            r = await _run(["tmux", "send-keys", "-t", session, key])
        except FileNotFoundError:
            _fail(502, "unexpected", "tmux no disponible")
        if r.returncode != 0:
            _tmux_failed(tty, r.stderr.strip() or str(r.returncode))
        await asyncio.sleep(0.05)
    detail = {"command": command, "user": user or None}
    if forced:
        detail.update(_forced_by(body, request))
    cursor = _record(state, "command", detail, cursor)
    return {"ok": True, "command": command, "session": session, "cursor": cursor}


@app.post("/api/device/{tty}/devremote-reset")
async def devremote_reset(tty: str, body: Optional[dict] = Body(None), authorization: Optional[str] = Header(None),
                          request: Request = None):
    """Mata y relanza el proceso de la placa. Como send/command: una reserva
    ajena lo bloquea (423, `force` del dashboard), `require_reservation` y
    `expect_mac` del CLI. Queda un evento `command` (command=restart-session,
    con quién forzó si se forzó), con el cursor previo: es el fin de esa sesión."""
    _require_auth(authorization)
    _check_tty(tty)   # ttyUSBN o esp-slotK: devremote resuelve el nombre.
    body = body if isinstance(body, dict) else {}
    state = runstate.read(tty) or {}
    _check_expect_mac(state, body.get("expect_mac"))
    forced = _check_reservation(tty, body)
    user, _ = _creds(body, required=False)
    cursor = events.log_end_cursor(state["log_path"]) if state.get("log_path") else None
    result = await _run(["/usr/local/bin/devremote", "--reset", tty])
    if result.returncode == 0 and cursor is not None:
        detail = {"command": "restart-session", "user": user or None}
        if forced:
            detail.update(_forced_by(body, request))
        _record(state, "command", detail, cursor)
    return {"ok": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr}


# ---------- update del bench (espbench-update) ----------

@app.get("/api/update")
async def get_update():
    """Último espbench-update (`status`, None si nunca corrió) y si el bench está
    fijo en una rama/tag/commit (`pin`; None = sigue los releases)."""
    return {"version": _read_first_line(paths.version_file()) or "dev",
            "pin": bench_update.read_pin(), "status": bench_update.read_status()}


@app.post("/api/update")
async def post_update(body: Optional[dict] = Body(None), authorization: Optional[str] = Header(None)):
    """Actualiza el bench en segundo plano. `{"ref": "<rama|tag|commit>"}` lo deja
    fijo ahí (para probar algo sin release); sin `ref`, al último release y sigue
    los releases. Reinicia el dashboard: el resultado se ve en GET /api/update."""
    _require_auth(authorization)
    body = body if isinstance(body, dict) else {}
    ref = str(body.get("ref") or "").strip()
    force = body.get("force") is True
    if ref and not bench_update.valid_ref(ref):
        _fail(400, "bad_request", f"ref no válida: {ref!r}")
    st = bench_update.read_status() or {}
    if st.get("state") == "running":
        _fail(409, "busy", f"ya hay un update corriendo ({st.get('message', '')})")
    if not force:
        reason = bench_update.busy_reason()
        if reason:
            _fail(409, "busy", f"bench ocupado: {reason} (force: true para actualizar igual)")
    unit = f"espbench-update-manual-{int(time.time())}"
    try:
        r = await _run(bench_update.start_command(ref or None, force=force, unit=unit))
    except FileNotFoundError as e:
        _fail(502, "update_unavailable", f"no se pudo lanzar el update: {e}")
    if r.returncode != 0:
        _fail(502, "update_unavailable", f"no se pudo lanzar el update: {(r.stderr or r.stdout).strip()}")
    taglog.info(TAG, f"update pedido: {ref or 'último release'} (unit {unit})")
    return {"ok": True, "ref": ref or None, "unit": unit}


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

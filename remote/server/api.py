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
from typing import Any
from urllib.parse import unquote

from fastapi import Body, FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from server import history, paths, runstate
from server.device_registry import DeviceRegistry, DevicesFile
from server.log_streamer import LogStreamer

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


def _check_tty(tty: str) -> None:
    if not _TTY_RE.fullmatch(tty):
        raise HTTPException(status_code=400, detail=f"tty no válido: {tty}")


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
async def device_send(tty: str, body: dict = Body(...)):
    """Manda texto por el serial del device (a través del monitor en tmux)."""
    _check_tty(tty)
    text = str(body.get("text", ""))
    enter = bool(body.get("enter", True))
    if len(text) > SEND_MAX:
        raise HTTPException(status_code=400, detail=f"texto de más de {SEND_MAX} caracteres")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in text):
        raise HTTPException(status_code=400, detail="caracteres de control no permitidos")
    if not text and not enter:
        raise HTTPException(status_code=400, detail="nada para mandar")
    state = runstate.read(tty) or {}
    if state.get("state") in BUSY_STATES:
        raise HTTPException(status_code=409, detail=f"device ocupado ({state['state']})")
    session = f"esp32_{tty}"
    for cmd in send_keys_cmds(session, text, enter):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise HTTPException(status_code=502, detail=f"tmux: {r.stderr.strip() or r.returncode}")
    return {"ok": True, "session": session, "sent": text, "enter": enter}


@app.get("/api/device/{tty:path}")
async def get_device(tty: str):
    device = registry.get_device(tty)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{tty}' not found")
    return dataclasses.asdict(device)


_devices_file = DevicesFile()


@app.patch("/api/devices/{mac:path}")
async def patch_device(mac: str, body: dict = Body(...)):
    device_key = body.get("device_key", "").strip()
    if not device_key:
        raise HTTPException(status_code=400, detail="device_key requerido")
    mac_norm = unquote(mac).upper().replace("-", ":")
    try:
        _devices_file.update_device_key(mac_norm, device_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


@app.post("/api/device/{tty}/unlock")
async def device_unlock(tty: str, body: dict = Body(...)):
    lock_user = body.get("lock_user", "").strip()
    lock_token = body.get("lock_token", "").strip()
    if not lock_user or not lock_token:
        raise HTTPException(status_code=400, detail="lock_user y lock_token requeridos")
    lock_file = paths.lock_file(tty)
    if not lock_file.exists():
        return {"ok": True, "message": "no estaba bloqueado"}
    parts = lock_file.read_text().strip().split(':', 1)
    stored_user = parts[0]
    stored_token = parts[1] if len(parts) > 1 else ''
    if stored_user != lock_user or stored_token != lock_token:
        raise HTTPException(status_code=403, detail="par user/token incorrecto")
    lock_file.unlink()
    return {"ok": True, "message": "desbloqueado"}


_COMMANDS = {
    "reset":      ["C-t", "C-r"],  # Ctrl+T Ctrl+R — reset via RTS
    "bootloader": ["C-t", "C-p"],  # Ctrl+T Ctrl+P — reset into bootloader
}

@app.post("/api/device/{tty}/command/{command}")
async def device_command(tty: str, command: str):
    if command not in _COMMANDS:
        raise HTTPException(status_code=400, detail=f"Comando desconocido: {command}")
    session = f"esp32_{tty}"
    for key in _COMMANDS[command]:
        subprocess.run(["tmux", "send-keys", "-t", session, key], check=False)
        await asyncio.sleep(0.05)
    return {"ok": True, "command": command, "session": session}


@app.post("/api/device/{tty}/devremote-reset")
async def devremote_reset(tty: str):
    _check_tty(tty)   # ttyUSBN o esp-slotK: devremote resuelve el nombre.
    result = subprocess.run(
        ["/usr/local/bin/devremote", "--reset", tty],
        capture_output=True, text=True
    )
    return {"ok": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr}


@app.websocket("/ws/device/{tty:path}")
async def ws_device(websocket: WebSocket, tty: str):
    await streamer.subscribe(tty, websocket)


app.mount("/", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

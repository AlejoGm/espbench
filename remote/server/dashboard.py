import asyncio
import dataclasses
import pathlib
import subprocess
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket
from fastapi.staticfiles import StaticFiles

from server.device_registry import DeviceRegistry
from server.log_streamer import LogStreamer

BASE_DIR = pathlib.Path(__file__).parent.parent
DASHBOARD_DIR = BASE_DIR / "dashboard"
_VERSION_FILE = pathlib.Path("/opt/esp/VERSION")

app = FastAPI()
registry = DeviceRegistry()
streamer = LogStreamer(registry=registry)


@app.get("/api/version")
async def get_version():
    try:
        version = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "dev"
    except Exception:
        version = "dev"
    return {"version": version}


@app.get("/api/devices")
async def get_devices():
    return [dataclasses.asdict(d) for d in registry.list_devices()]


@app.get("/api/device/{tty:path}")
async def get_device(tty: str):
    device = registry.get_device(tty)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{tty}' not found")
    return dataclasses.asdict(device)


_LOCKS_DIR = pathlib.Path("/opt/esp/locks")

@app.post("/api/device/{tty}/unlock")
async def device_unlock(tty: str, body: dict = Body(...)):
    lock_user = body.get("lock_user", "").strip()
    lock_token = body.get("lock_token", "").strip()
    if not lock_user or not lock_token:
        raise HTTPException(status_code=400, detail="lock_user y lock_token requeridos")
    lock_file = _LOCKS_DIR / tty
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


@app.websocket("/ws/device/{tty:path}")
async def ws_device(websocket: WebSocket, tty: str):
    await streamer.subscribe(tty, websocket)


app.mount("/", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

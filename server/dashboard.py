import dataclasses
import pathlib

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.staticfiles import StaticFiles

from server.device_registry import DeviceRegistry
from server.log_streamer import LogStreamer

BASE_DIR = pathlib.Path(__file__).parent.parent
DASHBOARD_DIR = BASE_DIR / "dashboard"

app = FastAPI()
registry = DeviceRegistry()
streamer = LogStreamer(registry=registry)


@app.get("/api/devices")
async def get_devices():
    return [dataclasses.asdict(d) for d in registry.list_devices()]


@app.get("/api/device/{tty:path}")
async def get_device(tty: str):
    device = registry.get_device(tty)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{tty}' not found")
    return dataclasses.asdict(device)


@app.websocket("/ws/device/{tty:path}")
async def ws_device(websocket: WebSocket, tty: str):
    await streamer.subscribe(tty, websocket)


app.mount("/", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

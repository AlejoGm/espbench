"""
Tests para LogStreamer: tail async, broadcast WebSocket, parsing de CHIPID.
"""

import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from server.log_streamer import LogStreamer


# ---------------------------------------------------------------------------
# Mock WebSocket
# ---------------------------------------------------------------------------

class MockWebSocket:
    """Simula la interfaz mínima de un WebSocket de FastAPI."""

    def __init__(self):
        self.sent: list[str] = []
        self.accepted = False
        self._closed = False

    async def accept(self):
        self.accepted = True

    async def send_text(self, text: str):
        if self._closed:
            raise RuntimeError("WebSocket closed")
        self.sent.append(text)

    def close_ws(self):
        self._closed = True


# ---------------------------------------------------------------------------
# Mock Registry
# ---------------------------------------------------------------------------

class MockRegistry:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def set_chip_id(self, tty_name: str, chip_id: str):
        self.calls.append((tty_name, chip_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_log_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Crea el directorio de hoy y devuelve la ruta del log."""
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    day_dir = tmp_path / today
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir / "serial.log"


async def subscribe_and_cancel(streamer, tty, ws, cancel_after=0.05):
    """Suscribe el websocket y cancela después de cancel_after segundos."""
    task = asyncio.get_event_loop().create_task(streamer.subscribe(tty, ws))
    await asyncio.sleep(cancel_after)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_subscribe_sends_existing_content(tmp_path):
    """Al suscribir, el cliente recibe el contenido completo del log existente."""
    log_path = make_log_path(tmp_path)
    log_path.write_text("boot log line 1\nboot log line 2\n")

    ws = MockWebSocket()
    streamer = LogStreamer(logs_base=str(tmp_path))

    asyncio.run(subscribe_and_cancel(streamer, "ttyUSB0", ws, cancel_after=0.05))

    full_sent = "".join(ws.sent)
    assert "boot log line 1" in full_sent
    assert "boot log line 2" in full_sent


def test_subscribe_empty_file_sends_nothing(tmp_path):
    """Si el archivo existe pero está vacío, no se envía nada inicial."""
    log_path = make_log_path(tmp_path)
    log_path.write_text("")

    ws = MockWebSocket()
    streamer = LogStreamer(logs_base=str(tmp_path))

    asyncio.run(subscribe_and_cancel(streamer, "ttyUSB0", ws, cancel_after=0.05))

    # No debe haber enviado nada (o solo strings vacíos)
    assert all(s == "" for s in ws.sent) or ws.sent == []


def test_subscribe_no_file_sends_nothing(tmp_path):
    """Si el archivo no existe al suscribir, no se envía nada inicial."""
    # No creamos el log file
    ws = MockWebSocket()
    streamer = LogStreamer(logs_base=str(tmp_path))

    asyncio.run(subscribe_and_cancel(streamer, "ttyUSB0", ws, cancel_after=0.05))

    assert ws.sent == []


def test_chipid_parsed_and_set(tmp_path):
    """CHIPID en el log inicial llama a registry.set_chip_id con el valor correcto."""
    log_path = make_log_path(tmp_path)
    log_path.write_text("I (123) boot: starting\nCHIPID = 99887766\nI (124) app: ready\n")

    registry = MockRegistry()
    ws = MockWebSocket()
    streamer = LogStreamer(logs_base=str(tmp_path), registry=registry)

    asyncio.run(subscribe_and_cancel(streamer, "ttyUSB0", ws, cancel_after=0.05))

    assert ("ttyUSB0", "99887766") in registry.calls


def test_new_lines_streamed(tmp_path):
    """Líneas escritas al archivo después de suscribir llegan al WebSocket."""
    log_path = make_log_path(tmp_path)
    log_path.write_text("")  # archivo vacío al inicio

    ws = MockWebSocket()
    streamer = LogStreamer(logs_base=str(tmp_path))

    async def run():
        task = asyncio.get_event_loop().create_task(streamer.subscribe("ttyUSB0", ws))
        # Esperar a que la tarea arranque y lea la posición inicial
        await asyncio.sleep(0.1)
        # Escribir nueva línea
        with open(log_path, "a") as f:
            f.write("nueva linea de log\n")
        # Esperar a que el tail la detecte (polling cada 0.2s)
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(run())

    full_sent = "".join(ws.sent)
    assert "nueva linea de log" in full_sent


def test_chipid_in_new_content(tmp_path):
    """CHIPID en líneas nuevas (no en el contenido inicial) también se detecta."""
    log_path = make_log_path(tmp_path)
    log_path.write_text("")

    registry = MockRegistry()
    ws = MockWebSocket()
    streamer = LogStreamer(logs_base=str(tmp_path), registry=registry)

    async def run():
        task = asyncio.get_event_loop().create_task(streamer.subscribe("ttyUSB0", ws))
        await asyncio.sleep(0.1)
        with open(log_path, "a") as f:
            f.write("CHIPID = 11223344\n")
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(run())

    assert ("ttyUSB0", "11223344") in registry.calls


def test_disconnect_does_not_affect_other_subscriber(tmp_path):
    """La desconexión de un cliente no rompe a los otros suscriptores."""
    log_path = make_log_path(tmp_path)
    log_path.write_text("")

    ws1 = MockWebSocket()
    ws2 = MockWebSocket()
    streamer = LogStreamer(logs_base=str(tmp_path))

    async def run():
        task1 = asyncio.get_event_loop().create_task(streamer.subscribe("ttyUSB0", ws1))
        task2 = asyncio.get_event_loop().create_task(streamer.subscribe("ttyUSB0", ws2))

        await asyncio.sleep(0.15)

        # Cerrar ws1 (simula desconexión en el broadcast)
        ws1.close_ws()

        # Escribir algo: ws1 fallará silenciosamente, ws2 debe recibirlo
        with open(log_path, "a") as f:
            f.write("contenido para ws2\n")

        await asyncio.sleep(0.5)

        task1.cancel()
        task2.cancel()
        for t in [task1, task2]:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(run())

    full_sent_ws2 = "".join(ws2.sent)
    assert "contenido para ws2" in full_sent_ws2

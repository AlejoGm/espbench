"""
Log streamer: tail async del log de un dispositivo con broadcast a WebSockets.
Parsea CHIPID e info de firmware en el stream y notifica al DeviceRegistry.

Qué archivo leer lo dice el proceso del device en run/<tty>.json (log_path):
el DeviceLog vive en devices/<mac>/ y cambia de archivo en cada sesión. Sin
estado runtime (o en tests con logs_base explícito) cae al esquema anterior,
logs/<tty>/output.log.
"""

import asyncio
import codecs
import pathlib
import re
import sys
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from server import paths, runstate

CHIPID_RE     = re.compile(r"CHIPID\s*=\s*(\d+)")
FW_PROJECT_RE = re.compile(r"app_init: Project name:\s+(\S+)")
FW_VERSION_RE = re.compile(r"app_init: App version:\s+(\S+)")
FW_IDF_RE     = re.compile(r"app_init: ESP-IDF:\s+(\S+)")
_ANSI_RE      = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s).strip()


class LogStreamer:
    """
    Mantiene una tarea de tail por ttyUSBX y hace broadcast a todos los
    WebSocket suscritos a ese tty.
    """

    def __init__(self, logs_base: Optional[str] = None, registry=None):
        self._explicit_base = logs_base is not None
        self._logs_base = pathlib.Path(logs_base) if logs_base else paths.logs_dir()
        self._registry = registry
        # tty_name → set of websockets
        self._subscribers: dict[str, set] = {}
        # tty_name → asyncio.Task (tail loop)
        self._tasks: dict[str, asyncio.Task] = {}
        # tty_name → asyncio.Lock
        self._locks: dict[str, asyncio.Lock] = {}

    def _log_path(self, tty_name: str) -> pathlib.Path:
        if not self._explicit_base:
            state = runstate.read(tty_name)
            if state and state.get("log_path"):
                return pathlib.Path(state["log_path"])
        return self._logs_base / tty_name / "output.log"

    def _ensure_lock(self, tty_name: str) -> asyncio.Lock:
        if tty_name not in self._locks:
            self._locks[tty_name] = asyncio.Lock()
        return self._locks[tty_name]

    async def subscribe(self, tty_name: str, websocket) -> None:
        """
        Conecta un WebSocket al stream del tty dado.
        1. Envía el contenido actual del log.
        2. Arranca la tarea de tail si no existe.
        3. Queda registrado hasta que el WebSocket se desconecte.
        """
        await websocket.accept()

        lock = self._ensure_lock(tty_name)
        async with lock:
            if tty_name not in self._subscribers:
                self._subscribers[tty_name] = set()
            self._subscribers[tty_name].add(websocket)

        # Enviar contenido existente del log
        log_path = self._log_path(tty_name)
        if log_path.exists():
            with open(log_path, "r", errors="replace", newline='') as f:
                content = f.read()
            if content:
                self._check_chipid(tty_name, content)
                try:
                    await websocket.send_text(content)
                except Exception:
                    pass

        # Arrancar la tarea de tail si no está corriendo
        if tty_name not in self._tasks or self._tasks[tty_name].done():
            task = asyncio.get_event_loop().create_task(
                self._tail_loop(tty_name)
            )
            self._tasks[tty_name] = task

        # Esperar hasta que el websocket se desconecte.
        # Usar receive() con timeout para detectar cierres silenciosos (zombie WS).
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(websocket.receive(), timeout=30.0)
                    if msg.get("type") == "websocket.disconnect":
                        break
                except asyncio.TimeoutError:
                    pass  # sigue vivo — continuar
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            await self.unsubscribe(tty_name, websocket)

    async def unsubscribe(self, tty_name: str, websocket) -> None:
        lock = self._ensure_lock(tty_name)
        async with lock:
            subs = self._subscribers.get(tty_name)
            if subs is not None:
                subs.discard(websocket)
                if not subs:
                    del self._subscribers[tty_name]
                    task = self._tasks.pop(tty_name, None)
                    if task is not None and not task.done():
                        task.cancel()

    async def _tail_loop(self, tty_name: str) -> None:
        """
        Tarea de background: lee el log file desde el final y hace broadcast
        de los nuevos bytes a todos los suscriptores del tty.
        """
        log_path = self._log_path(tty_name)
        position, inode = _size_and_inode(log_path)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        while True:
            try:
                await asyncio.sleep(0.2)

                # El archivo cambia con cada sesión del device (rotación) y cuando
                # una MAC resuelta tarde migra el log del provisorio al de la MAC.
                log_path = self._log_path(tty_name)
                if not log_path.exists():
                    continue

                current_size, current_inode = _size_and_inode(log_path)
                if current_inode != inode or current_size < position:
                    # Archivo nuevo o truncado: leerlo desde el principio. Antes se
                    # salteaba hasta el final y se perdía el arranque de la sesión.
                    inode, position = current_inode, 0
                    decoder.reset()
                if current_size == position:
                    continue

                # Binario + posición por bytes leídos: si el archivo crece entre el
                # stat() y el read(), lo leído de más no se vuelve a mandar.
                with open(log_path, "rb") as f:
                    f.seek(position)
                    data = f.read()
                position += len(data)
                new_content = decoder.decode(data)

                if not new_content:
                    continue

                self._check_chipid(tty_name, new_content)
                await self._broadcast(tty_name, new_content)

            except asyncio.CancelledError:
                return
            except Exception:
                # No romper el loop por errores de lectura
                await asyncio.sleep(0.5)

    async def _broadcast(self, tty_name: str, text: str) -> None:
        lock = self._ensure_lock(tty_name)
        async with lock:
            subs = set(self._subscribers.get(tty_name, set()))

        dead = set()
        for ws in subs:
            try:
                await ws.send_text(text)
            except Exception:
                dead.add(ws)

        if dead:
            async with lock:
                existing = self._subscribers.get(tty_name, set())
                existing -= dead

    def scan_all(self, dev_dir: str = "/dev") -> None:
        """Parse fw info from existing output.log for every ttyUSBX. Call at startup."""
        for entry in sorted(pathlib.Path(dev_dir).glob("ttyUSB*")):
            log_path = self._log_path(entry.name)
            if log_path.exists():
                try:
                    self._check_chipid(entry.name, log_path.read_text(errors="replace"))
                except Exception:
                    pass

    def _check_chipid(self, tty_name: str, text: str) -> None:
        if self._registry is None:
            return
        clean = _strip_ansi(text)
        m = CHIPID_RE.search(clean)
        if m:
            self._registry.set_chip_id(tty_name, m.group(1))
        m_proj = FW_PROJECT_RE.search(clean)
        m_ver  = FW_VERSION_RE.search(clean)
        m_idf  = FW_IDF_RE.search(clean)
        if m_proj or m_ver or m_idf:
            self._registry.set_firmware_info(
                tty_name,
                project=m_proj.group(1) if m_proj else None,
                version=m_ver.group(1)  if m_ver  else None,
                idf=m_idf.group(1)      if m_idf  else None,
            )


def _size_and_inode(path: pathlib.Path):
    try:
        st = path.stat()
        return st.st_size, st.st_ino
    except OSError:
        return 0, None

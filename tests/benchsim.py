"""
benchsim.py — banco simulado para probar el cliente (espbench_lib / CLI) sin Pi.

Todo real salvo los bordes:
- SimBoard: DeviceManager + DeviceLog + SerialWatch reales (run/<tty>.json,
  devices/<MAC>/output.log, events.jsonl). El "firmware" es un hilo que escribe
  serial: boot, prompt de esp_console, eco de lo que se teclea, respuestas,
  panic.
- tmux falso: el subprocess.run de api.py → send-keys le escribe a la SimBoard (eco +
  respuesta), C-t C-r la resetea, devremote --reset la "re-enchufa" (sesión
  nueva).
- Flash: protocol.serve_connection real en un socket TCP, con esptool falso.
- API: server.api real, servida por un adaptador http.server que llama a los
  handlers (no hace falta uvicorn) o por uvicorn si está instalado.

Uso en tests: ver tests/test_espbench_lib.py. A mano (smoke end-to-end):

    ESP_BASE=$(mktemp -d) python -m tests.benchsim --port 8099 [--uvicorn]
"""
import asyncio
import inspect
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "remote"))

from common import mac_to_sn_sfy  # noqa: E402
from server import api, device_log, protocol, taglog  # noqa: E402
from server.device import DeviceManager  # noqa: E402
from server.device_registry import DeviceRegistry, DevicesFile  # noqa: E402
from server.flash import build_esptool_cmd  # noqa: E402

PROMPT = "esp> "
PANIC = ("Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled.\r\n"
         "\r\nBacktrace: 0x400d1234:0x3ffb0000 0x400d5678:0x3ffb0020\r\n\r\nRebooting...\r\n")


def boot_text(reason: str = "SW_CPU_RESET", code: str = "0xc", version: str = "1.0.0") -> str:
    return (f"ets Jun  8 2016 00:22:57\r\n\r\nrst:{code} ({reason}),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n"
            "I (29) boot: ESP-IDF v5.3 2nd stage bootloader\r\n"
            "I (31) app_init: Project name:     simfw\r\n"
            f"I (32) app_init: App version:      {version}\r\n"
            "I (33) app_init: ESP-IDF:          v5.3\r\n"
            "I (120) main: listo\r\n")


class SimMonitor:
    """El monitor que pausa/relanza protocol.monitor_paused. Al relanzarlo, el
    firmware nuevo arranca (board.after_flash)."""

    def __init__(self, board):
        self.board = board
        self.calls = []

    def stop(self):
        self.calls.append("stop")

    def start(self):
        self.calls.append("start")
        self.board.later(0.05, self.board.after_flash)


class SimTools(protocol.FlashTools):
    def __init__(self, board):
        self.board = board
        super().__init__(find_esptool=lambda: ["esptool"], read_mac=lambda tty: board.mac,
                         build_cmd=build_esptool_cmd, run=self._run)

    def _run(self, cmd, log=None, on_line=None):
        if on_line:
            on_line("Writing at 0x00010000... (100 %)")
            on_line("Hard resetting via RTS pin...")
        return self.board.flash_rc


class SimBoard:
    """Una placa enchufada a la Pi simulada."""

    def __init__(self, bench, tty: str = "ttyUSB0", mac: str = "AA:BB:CC:DD:EE:01", key=None):
        self.bench, self.tty, self.mac = bench, tty, mac
        self.responses = {"status": ["OK uptime=12s heap=210000"], "version": ["simfw 1.0.0"]}
        self.reply_delay = 0.05
        self.flash_rc = 0
        self.after_flash = self.boot             # lo que hace el firmware nuevo
        self.flashes = 0
        self._ctrl_t = False
        self._timers = []
        (bench.dev_dir / tty).touch()
        sn = mac_to_sn_sfy(mac)
        DevicesFile().register_mac(mac, sn)
        if key:
            DevicesFile().update_device_key(mac, key)
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(5)
        self.port = self._srv.getsockname()[1]
        self.monitor = SimMonitor(self)
        self.manager = None
        self._start_process()
        threading.Thread(target=self._serve_flash, daemon=True).start()

    # ----- proceso de la placa -----

    def _start_process(self):
        self.manager = DeviceManager(f"/dev/{self.tty}", mac_reader=lambda: self.mac, tcp_port=self.port)
        # El mismo mecanismo de línea parcial, con otro tiempo (tests rápidos; el real es 150 ms)
        self.manager.device.device_log._partial_timeout = self.bench.partial_timeout
        self.manager.discover()

    @property
    def device(self):
        return self.manager.device

    def _serve_flash(self):
        cfg = {"tty": f"/dev/{self.tty}", "token": "", "chip": "esp32", "flash_baud": 921600, "port": self.port}
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self.flashes += 1
            protocol.serve_connection(conn, cfg, self.monitor, self.device, SimTools(self))

    def later(self, delay: float, fn, *args):
        t = threading.Timer(delay, fn, args)
        t.daemon = True
        self._timers.append(t)
        t.start()

    # ----- firmware -----

    def serial(self, text: str) -> None:
        self.manager.on_serial(text.encode("utf-8"))

    def boot(self, reason: str = "SW_CPU_RESET", code: str = "0xc") -> None:
        self.serial(boot_text(reason, code))
        self.serial(PROMPT)

    def wait_prompt(self, timeout: float = 2.0) -> None:
        """Hasta que el prompt (sin \\n: sale al archivo a los partial_timeout) esté escrito."""
        log = self.device.device_log.path
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if log is not None and log.read_bytes().endswith(f"> {PROMPT}\n".encode()):
                return
            time.sleep(0.01)
        raise TimeoutError("el prompt no salió al log")

    def panic(self) -> None:
        self.serial(PANIC)
        self.boot("SW_CPU_RESET")

    def boot_then_panic(self, delay: float = 0.3) -> None:
        self.boot()
        self.later(delay, self.panic)

    def replug(self, then_boot: bool = True) -> None:
        """USB-Serial-JTAG (S3/C3), o devremote --reset: el proceso termina y
        arranca otro (sesión nueva) para la misma placa."""
        self.device.disconnect()
        time.sleep(0.05)
        self._start_process()
        if then_boot:
            self.boot("POWERON_RESET", "0x1")

    def replug_then_boot(self) -> None:
        self.later(0.3, self.replug)

    # ----- tmux falso -----

    def keys(self, text: str) -> None:
        self._typed = getattr(self, "_typed", "") + text
        self.serial(text)                       # eco de esp_console

    def enter(self) -> None:
        cmd, self._typed = getattr(self, "_typed", "").strip(), ""
        self.serial("\r\n")
        self.later(self.reply_delay, self._reply, cmd)

    def _reply(self, cmd: str) -> None:
        if cmd == "panic":
            self.panic()
            return
        if cmd == "reboot":
            self.boot()
            return
        for line in self.responses.get(cmd, [f"error: comando desconocido '{cmd}'"]):
            self.serial(line + "\r\n")
        self.serial(PROMPT)

    def key(self, key: str) -> None:
        if key == "C-t":
            self._ctrl_t = True
            return
        if self._ctrl_t and key == "C-r":
            self.later(0.02, self.boot, "RTCWDT_RTC_RESET", "0x10")
        elif self._ctrl_t and key == "C-p":
            self.later(0.02, self.serial, "rst:0x1 (POWERON_RESET),boot:0x3 (DOWNLOAD_BOOT(UART0/UART1/SDIO))\r\n"
                                          "waiting for download\r\n")
        self._ctrl_t = False

    def close(self):
        for t in self._timers:
            t.cancel()
        try:
            self._srv.close()
        except OSError:
            pass
        try:
            self.device.device_log.close()
        except Exception:
            pass


class _FakeRun:
    """api.subprocess.run: tmux send-keys y devremote van a las SimBoard."""

    def __init__(self, bench):
        self.bench = bench
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["tmux", "send-keys"]:
            board = self.bench.boards.get(cmd[cmd.index("-t") + 1][len("esp32_"):])
            if board is None:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="no server running")
            if "-l" in cmd:
                board.keys(cmd[-1])
            elif cmd[-1] == "Enter":
                board.enter()
            else:
                board.key(cmd[-1])
            return ok
        if cmd and str(cmd[0]).endswith("devremote") and "--reset" in cmd:
            board = self.bench.boards.get(cmd[-1])
            if board is not None:
                board.later(0.05, board.replug)
            return ok
        return types.SimpleNamespace(returncode=1, stdout="", stderr=f"comando no simulado: {cmd}")


# ---------- API: adaptador http.server → handlers de server.api ----------

def _call(fn, *args, **kw):
    r = fn(*args, **kw)
    return asyncio.run(r) if inspect.iscoroutine(r) else r


def _query_kwargs(fn, query: dict) -> dict:
    """Como FastAPI: solo los parámetros que el handler declara."""
    params = inspect.signature(fn).parameters
    out = {}
    for k, v in query.items():
        if k in params and k not in ("key", "tty", "body", "authorization"):
            out[k] = v in ("1", "true", "True") if params[k].annotation is bool else v
    return out


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status: int, obj) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method: str) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        parts = [urllib.parse.unquote(p) for p in url.path.strip("/").split("/")]
        auth = self.headers.get("Authorization")
        body = None
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self._send(422, {"detail": "body JSON inválido"})
        try:
            result = self._route(method, parts, query, body, auth)
        except api.HTTPException as e:
            return self._send(e.status_code, {"detail": e.detail})
        if result is _NOT_FOUND:
            return self._send(404, {"detail": "Not Found"})
        self._send(200, result)

    def _route(self, method, parts, query, body, auth):
        if method == "GET" and parts == ["api", "devices"]:
            return _call(api.get_devices)
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "board"]:
            fn = {"log": api.board_log, "events": api.board_events}.get(parts[3])
            if fn is not None:
                return _call(fn, parts[2], **_query_kwargs(fn, query))
        if parts[:2] == ["api", "device"] and len(parts) >= 3:
            tty = parts[2]
            if method == "GET" and len(parts) == 3:
                return _call(api.get_device, tty)
            if method == "POST" and len(parts) == 4:
                fn = {"send": api.device_send, "reserve": api.device_reserve, "release": api.device_release,
                      "unlock": api.device_unlock}.get(parts[3])
                if fn is not None:
                    return _call(fn, tty, body, authorization=auth)
                if parts[3] == "devremote-reset":
                    return _call(api.devremote_reset, tty, authorization=auth)
            if method == "POST" and len(parts) == 5 and parts[3] == "command":
                return _call(api.device_command, tty, parts[4], body, authorization=auth)
        return _NOT_FOUND

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


_NOT_FOUND = object()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Bench:
    """La Pi simulada. ESP_BASE tiene que estar seteado antes (tests: conftest)."""

    def __init__(self, server: str = "adapter", port: int = 0, partial_timeout: float = 0.03):
        """partial_timeout: hold de la línea serial sin \\n de DeviceLog (el real
        es device_log.PARTIAL_TIMEOUT, 150 ms; los tests usan menos)."""
        self.server_kind = server
        self.partial_timeout = partial_timeout
        self.base = pathlib.Path(os.environ["ESP_BASE"])
        self.dev_dir = self.base / "_dev"
        self.dev_dir.mkdir(parents=True, exist_ok=True)
        self.boards = {}
        self._port = port
        self._saved = {}
        self._sid_n = 0
        self.run = _FakeRun(self)

    # ----- ciclo de vida -----

    def start(self) -> "Bench":
        self._saved = {"registry": api.registry, "subprocess": api.subprocess,
                       "sid": device_log.make_session_id}
        api.registry = DeviceRegistry(dev_dir=str(self.dev_dir))
        # Solo el subprocess que ve api.py: api.subprocess es el módulo global, y
        # pisar su .run rompería todo subprocess del proceso (los tests del CLI).
        api.subprocess = types.SimpleNamespace(run=self.run, PIPE=subprocess.PIPE)
        orig = self._saved["sid"]

        def unique_sid(epoch, pid):
            # Varias sesiones en el mismo proceso y el mismo segundo (la Pi real: otro pid)
            self._sid_n += 1
            return f"{orig(epoch, pid)}{self._sid_n:03d}"
        device_log.make_session_id = unique_sid
        taglog.clear_sinks()
        if self.server_kind == "uvicorn":
            self._start_uvicorn()
        else:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", self._port), _Handler)
            self._httpd.daemon_threads = True
            self.port = self._httpd.server_address[1]
            # poll_interval: shutdown() espera hasta un poll entero (default 0,5 s por test)
            threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        return self

    def _start_uvicorn(self):
        import uvicorn
        self.port = self._port or _free_port()
        config = uvicorn.Config(api.app, host="127.0.0.1", port=self.port, log_level="warning")
        self._uv = uvicorn.Server(config)
        self._uv_thread = threading.Thread(target=self._uv.run, daemon=True)
        self._uv_thread.start()
        deadline = time.monotonic() + 10
        while not self._uv.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn no arrancó")
            time.sleep(0.02)

    def close(self):
        for b in self.boards.values():
            b.close()
        if self.server_kind == "uvicorn":
            self._uv.should_exit = True
            self._uv_thread.join(timeout=5)
        else:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._saved:
            api.registry = self._saved["registry"]
            api.subprocess = self._saved["subprocess"]
            device_log.make_session_id = self._saved["sid"]
        taglog.reset_default_sinks()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def add_board(self, tty: str = "ttyUSB0", mac: str = "AA:BB:CC:DD:EE:01", key=None, boot: bool = True) -> SimBoard:
        b = SimBoard(self, tty, mac, key)
        self.boards[tty] = b
        if boot:
            b.boot("POWERON_RESET", "0x1")
            b.wait_prompt()
        return b


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Pi simulada para probar el CLI espbench")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--uvicorn", action="store_true")
    a = ap.parse_args(argv)
    if "ESP_BASE" not in os.environ:
        raise SystemExit("seteá ESP_BASE a un directorio temporal")
    bench = Bench("uvicorn" if a.uvicorn else "adapter", port=a.port,
                  partial_timeout=device_log.PARTIAL_TIMEOUT).start()
    bench.add_board("ttyUSB0", "AA:BB:CC:DD:EE:01", key="sim-board")
    print(f"bench en http://{bench.host} — placa sim-board en ttyUSB0 (Ctrl-C para salir)", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        bench.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

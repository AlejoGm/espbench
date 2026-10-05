"""
Tests para protocol.py — el flujo completo de un pedido de flash, sin hardware.

Cliente y server hablan por socket.socketpair(); esptool, la lectura de MAC y
el monitor son fakes. build_esptool_cmd es el real (lee flasher_args.json del
artefacto que se sube).
"""
import hashlib
import io
import json
import pathlib
import socket
import sys
import threading
import zipfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from common import recv_msg, send_msg
from server import protocol
from server.flash import build_esptool_cmd
from server.device import DeviceManager, DeviceState

TTY = "/dev/ttyUSB0"
MAC = "AA:BB:CC:DD:EE:FF"
CFG = {"tty": TTY, "token": "", "chip": "esp32", "flash_baud": 921600, "port": 5000}


def make_artifact(with_app=True, with_elf=True) -> bytes:
    files = {"0x1000": "bootloader.bin", "0x8000": "partition-table.bin"}
    if with_app:
        files["0x10000"] = "app.bin"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("flasher_args.json", json.dumps({"flash_files": files}))
        for name in files.values():
            z.writestr(name, b"\x00" * 16)
        if with_elf:
            z.writestr("firmware.elf", b"ELF")
    return buf.getvalue()


class FakeMonitor:
    def __init__(self):
        self.calls = []

    def stop(self):
        self.calls.append("stop")

    def start(self):
        self.calls.append("start")


class FakeTools(protocol.FlashTools):
    def __init__(self, rcs=(0,), mac=MAC, esptool_error=None, run_error=None):
        self.cmds = []
        self._rcs = list(rcs)
        self._mac = mac
        self._esptool_error = esptool_error
        self._run_error = run_error
        super().__init__(find_esptool=self._find, read_mac=lambda tty: self._mac,
                         build_cmd=build_esptool_cmd, run=self._run)

    def _find(self):
        if self._esptool_error:
            raise RuntimeError(self._esptool_error)
        return ["esptool"]

    def _run(self, cmd, log=None, on_line=None):
        self.cmds.append(cmd)
        if self._run_error:
            raise self._run_error
        if on_line:
            on_line("Writing at 0x00010000...")
        return self._rcs.pop(0) if self._rcs else 0


@pytest.fixture(autouse=True)
def esp_base(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    return tmp_path


def make_device(mac=MAC):
    manager = DeviceManager(TTY, mac_reader=lambda: mac, tcp_port=5000, publish_state=False)
    manager.discover()
    return manager.device


def request(header, payload=b"", tools=None, mon=None, cfg=CFG, device=None):
    """Corre un pedido completo contra serve_connection y devuelve todos los
    mensajes que recibió el cliente."""
    tools = tools or FakeTools()
    mon = mon or FakeMonitor()
    device = device or make_device()
    client, server = socket.socketpair()
    t = threading.Thread(target=protocol.serve_connection, args=(server, cfg, mon, device, tools))
    t.start()
    send_msg(client, header)
    msgs = []
    try:
        first = recv_msg(client)
        msgs.append(first)
        if first.get("phase") == "ready" and payload:
            client.sendall(payload)
        if first.get("phase") == "ready":
            while True:
                m = recv_msg(client)
                msgs.append(m)
                if "ok" in m:
                    break
    except ConnectionError:
        pass
    t.join(timeout=10)
    client.close()
    return msgs


def flash_header(payload, **extra):
    h = {"action": "upload_and_flash", "job_id": "job_20261005_120000_board1",
         "artifact_size": len(payload), "artifact_sha256": hashlib.sha256(payload).hexdigest(),
         "lock_user": "alejo", "lock_token": "t0k", "encrypt": True, "stream": True}
    h.update(extra)
    return h


def final(msgs):
    return msgs[-1]


# ---------- autenticación / acción / lock ----------

def test_unauthorized():
    msgs = request({"action": "upload_and_flash", "token": "mal"}, cfg={**CFG, "token": "bien"})
    assert msgs == [{"ok": False, "error": "unauthorized"}]


def test_bad_action():
    """deploy.py hace auth_ping con action 'xyz': tiene que recibir bad_action."""
    assert request({"action": "xyz"}) == [{"ok": False, "error": "bad_action"}]


def test_lock_credentials_required():
    msgs = request({"action": "upload_and_flash"})
    assert msgs[0]["error"] == "lock_credentials_required"


def test_device_locked_by_other_user(esp_base):
    (esp_base / "locks").mkdir()
    (esp_base / "locks" / "ttyUSB0").write_text("otro:xyz")
    msgs = request(flash_header(b"x"))
    assert msgs[0]["error"] == "device_locked"


def test_unlock(esp_base):
    (esp_base / "locks").mkdir()
    lock = esp_base / "locks" / "ttyUSB0"
    lock.write_text("alejo:t0k")
    assert request({"action": "unlock", "lock_user": "alejo", "lock_token": "mal"})[0]["error"] == "token_mismatch"
    assert lock.exists()
    assert request({"action": "unlock", "lock_user": "alejo", "lock_token": "t0k"})[0]["ok"] is True
    assert not lock.exists()


# ---------- flash ----------

def test_happy_path(esp_base):
    payload = make_artifact()
    mon, tools, device = FakeMonitor(), FakeTools(), make_device()
    states_during_flash = []
    real_run = tools._run
    tools.run = lambda cmd, log=None, on_line=None: states_during_flash.append(device.state) or real_run(cmd, log, on_line)
    msgs = request(flash_header(payload), payload, tools, mon, device=device)

    assert msgs[0] == {"ok": True, "phase": "ready", "job_id": "job_20261005_120000_board1"}
    streamed = [m["line"] for m in msgs if m.get("phase") == "log"]
    assert any("artifact OK" in l for l in streamed) and "Writing at 0x00010000..." in streamed
    done = final(msgs)
    assert done["ok"] and done["phase"] == "done" and done["status"] == "exitoso"
    assert done["missing_app"] is False and done["write_rc"] == 0
    assert mon.calls == ["stop", "start"]
    assert "--encrypt" in tools.cmds[0]
    assert states_during_flash == [DeviceState.FLASHING]
    assert device.state == DeviceState.MONITORING
    home = esp_base / "devices" / "AABBCCDDEEFF"
    jobdir = home / "jobs" / "job_20261005_120000_board1"
    assert (jobdir / "job.log").exists() and done["log_file"] == str(jobdir / "job.log")
    assert (home / "current.elf").read_bytes() == b"ELF"
    assert (home / "last_user").read_text() == "alejo"
    assert (esp_base / "locks" / "ttyUSB0").read_text() == "alejo:t0k"


def test_unknown_device_uses_tty_paths(esp_base):
    """Sin MAC (flash encryption, esptool no la lee) se flashea igual, con las
    rutas por tty de siempre, y la FSM vuelve a UNKNOWN."""
    payload = make_artifact()
    device = make_device(mac=None)
    done = final(request(flash_header(payload), payload, FakeTools(mac=None), device=device))
    assert done["ok"]
    assert device.state == DeviceState.UNKNOWN
    assert (esp_base / "jobs" / "job_20261005_120000_board1_ttyUSB0").is_dir()
    assert (esp_base / "current_ttyUSB0.elf").read_bytes() == b"ELF"
    assert (esp_base / "logs" / "ttyUSB0" / "last_user").read_text() == "alejo"


def test_unknown_device_adopts_mac_read_before_flash(esp_base):
    payload = make_artifact()
    device = make_device(mac=None)
    done = final(request(flash_header(payload), payload, FakeTools(mac=MAC), device=device))
    assert done["ok"] and device.mac == MAC
    assert device.state == DeviceState.MONITORING
    assert (esp_base / "devices" / "AABBCCDDEEFF" / "current.elf").exists()


def test_rejects_flash_while_erasing():
    """Antes, un flash en medio de un erase interactivo paraba el monitor igual."""
    device = make_device()
    device.start_erase()
    mon = FakeMonitor()
    msgs = request(flash_header(b"x"), tools=FakeTools(), mon=mon, device=device)
    assert msgs == [{"ok": False, "error": "device_busy", "message": "Device ocupado (erasing)"}]
    assert mon.calls == [] and device.state == DeviceState.ERASING


def test_retries_without_encrypt_on_rc2():
    payload = make_artifact()
    tools = FakeTools(rcs=[2, 0])
    done = final(request(flash_header(payload), payload, tools))
    assert done["ok"] and len(tools.cmds) == 2
    assert "--encrypt" in tools.cmds[0] and "--encrypt" not in tools.cmds[1]


def test_no_retry_when_encrypt_disabled():
    payload = make_artifact()
    tools = FakeTools(rcs=[2])
    done = final(request(flash_header(payload, encrypt=False), payload, tools))
    assert not done["ok"] and len(tools.cmds) == 1
    assert "puerto ocupado" in done["error_hint"]


def test_erase_runs_before_write():
    payload = make_artifact()
    tools = FakeTools()
    done = final(request(flash_header(payload, erase=True), payload, tools))
    assert done["ok"] and "erase-flash" in tools.cmds[0] and "write-flash" in tools.cmds[1]


def test_missing_app_is_partial():
    payload = make_artifact(with_app=False)
    done = final(request(flash_header(payload), payload))
    assert done["ok"] and done["missing_app"] and done["status"] == "parcial (sin aplicación)"


def test_failed_flash_does_not_copy_elf(esp_base):
    payload = make_artifact()
    done = final(request(flash_header(payload, encrypt=False), payload, FakeTools(rcs=[1])))
    assert not done["ok"] and done["status"] == "fallido"
    assert not (esp_base / "devices" / "AABBCCDDEEFF" / "current.elf").exists()


def test_sha256_mismatch_is_reported_as_exception():
    payload = make_artifact()
    msgs = request(flash_header(payload, artifact_sha256="00" * 32), payload)
    assert final(msgs)["error"] == "exception" and "SHA256" in final(msgs)["message"]


def test_device_changed_aborts_and_restarts_monitor():
    payload = make_artifact()
    mon, tools, device = FakeMonitor(), FakeTools(mac="11:22:33:44:55:66"), make_device()
    done = final(request(flash_header(payload), payload, tools, mon, device=device))
    assert done["error"] == "device_changed" and MAC in done["message"]
    assert tools.cmds == [] and mon.calls == ["stop", "start"]
    assert device.state == DeviceState.MONITORING


def test_esptool_not_found_restarts_monitor():
    payload = make_artifact()
    mon = FakeMonitor()
    done = final(request(flash_header(payload), payload, FakeTools(esptool_error="no hay esptool"), mon))
    assert done["error"] == "esptool_not_found" and mon.calls == ["stop", "start"]


def test_runner_crash_reports_rc_minus_one_and_restarts_monitor():
    payload = make_artifact()
    mon, device = FakeMonitor(), make_device()
    done = final(request(flash_header(payload), payload, FakeTools(run_error=OSError("boom")), mon, device=device))
    assert not done["ok"] and done["write_rc"] == -1
    assert done["error_hint"] == "Error interno al lanzar esptool."
    assert mon.calls == ["stop", "start"] and device.state == DeviceState.MONITORING


def test_upload_progress_is_not_logged_per_recv(tmp_path):
    """Regresión: con la condición "remaining < CHUNK_SIZE", en el último MB se
    logueaba CADA recv() (pocos KB): cientos de líneas en el log del device."""
    from server import taglog
    payload = b"x" * (11 * 1024 * 1024 + 12345)

    class TrickleSock:
        """recv() devuelve de a 4 KB, como un socket real con red de por medio."""
        def __init__(self, data):
            self.data, self.pos = data, 0

        def recv(self, n):
            chunk = self.data[self.pos:self.pos + min(n, 4096)]
            self.pos += len(chunk)
            return chunk

    lines = []
    taglog.add_sink(lambda ts, lvl, tag, msg: lines.append(msg) if "progreso" in msg else None)
    try:
        protocol.receive_artifact(TrickleSock(payload), {"artifact_size": len(payload)},
                                  "upload_and_flash", tmp_path / "artifact.zip")
    finally:
        taglog.reset_default_sinks()
    assert (tmp_path / "artifact.zip").stat().st_size == len(payload)
    assert 2 <= len(lines) <= 4, lines          # cada 5 MB + el final
    assert lines[-1].endswith(f"{len(payload)}/{len(payload)} bytes")

"""
Tests de integración del entrypoint: remote_esp32.main() con fakes solo en los
bordes (esptool, esp_idf_monitor, servidor TCP). Verifica el cableado de la
fase 3: DeviceManager → estado publicado → DeviceLog (serial + taglog) →
control server con el device → cierre.
"""
import json
import pathlib
import signal
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server import remote_esp32, runstate, taglog
from server.device import DeviceState

MAC = "AA:BB:CC:DD:EE:FF"


class FakeMon:
    instances = []

    def __init__(self, tty_path, baud, output_sink=None, elf_path=None, on_ctrl_e=None, input_sink=None):
        self.output_sink = output_sink
        self.input_sink = input_sink
        self.elf_path = elf_path
        self.on_ctrl_e = on_ctrl_e
        self.output = ""
        self.calls = []
        FakeMon.instances.append(self)

    def start(self):
        self.calls.append("start")
        self.output_sink(b"rst:0xc (SW_CPU_RESET),boot:0x13\r\nI (10) boot: ESP-IDF v5.3.2\r\n")

    def stop(self):
        self.calls.append("stop")

    def get_recent_output(self):
        return self.output


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    tty = tmp_path / "ttyUSB3"          # un archivo que existe hace de tty
    tty.touch()
    FakeMon.instances.clear()
    seen = {}

    def fake_control_server(cfg, mon, device):
        seen["cfg"], seen["device"] = cfg, device
        seen["state_at_start"] = runstate.read("ttyUSB3")

    monkeypatch.setattr(remote_esp32, "EspMonitor", FakeMon)
    monkeypatch.setattr(remote_esp32, "control_server", fake_control_server)
    monkeypatch.setattr(remote_esp32, "MAC_READ_DELAY", 0)
    remote_esp32._shutdown.clear()
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    yield tmp_path, tty, seen
    for s, h in old.items():
        signal.signal(s, h)
    taglog.reset_default_sinks()


def run_main(tty, base, stop_after=None):
    if stop_after is not None:
        threading.Timer(stop_after, remote_esp32._shutdown.set).start()
    remote_esp32.main(["-p", str(tty), "-tcp", "5003", "--base", str(base), "--chip", "esp32"])


def wait_for(cond, timeout=5.0):
    ev = threading.Event()
    end = threading.Timer(timeout, ev.set)
    end.start()
    while not ev.is_set():
        if cond():
            end.cancel()
            return True
        ev.wait(0.05)
    return False


def test_startup_identifies_publishes_and_logs(env, monkeypatch):
    base, tty, seen = env
    monkeypatch.setattr(remote_esp32, "read_mac", lambda port, stop=None: MAC)
    run_main(tty, base, stop_after=0.5)

    # el control server recibió el device ya identificado, con el estado publicado
    assert seen["device"].mac == MAC
    assert seen["cfg"]["port"] == 5003 and seen["cfg"]["chip"] == "esp32"
    st = seen["state_at_start"]
    assert st["state"] == "monitoring" and st["mac"] == MAC and st["tcp_port"] == 5003

    # serial (sink del monitor) y logs de taglog en el mismo archivo del device
    log = (base / "devices" / "AABBCCDDEEFF" / "output.log").read_text()
    assert "ESP-IDF v5.3.2" in log and "inicio: tty=" in log and "discovering -> monitoring" in log
    # y el mismo serial alimenta la salud del device
    assert seen["device"].snapshot()["health"]["last_reset"]["reason"] == "SW_CPU_RESET"

    # alta en devices.json
    assert MAC in json.loads((base / "devices.json").read_text())

    # cierre limpio: monitor detenido, estado runtime borrado
    assert FakeMon.instances[0].calls == ["start", "stop"]
    assert runstate.read("ttyUSB3") is None


def test_signal_ignored_while_flashing(env, monkeypatch):
    base, tty, seen = env
    monkeypatch.setattr(remote_esp32, "read_mac", lambda port, stop=None: MAC)

    def fake_control_server(cfg, mon, device):
        device.start_flash()
        remote_esp32._on_signal(signal.SIGTERM, None)   # llega una señal en medio del flash
        seen["shutdown_during_flash"] = remote_esp32._shutdown.is_set()
        device.finish_flash()
        remote_esp32._on_signal(signal.SIGTERM, None)   # ahora sí

    monkeypatch.setattr(remote_esp32, "control_server", fake_control_server)
    run_main(tty, base)
    assert seen["shutdown_during_flash"] is False


def test_disconnect_ends_process_and_leaves_state(env, monkeypatch):
    base, tty, seen = env
    monkeypatch.setattr(remote_esp32, "read_mac", lambda port, stop=None: MAC)
    threading.Timer(0.3, tty.unlink).start()        # se desenchufa
    run_main(tty, base)                              # termina solo, sin _shutdown externo
    st = runstate.read("ttyUSB3")
    assert st["state"] == "disconnected"             # esp32_tmux.sh lo usa para recrear la sesión


def test_mac_from_serial_when_esptool_cannot_read_it(env, monkeypatch):
    base, tty, seen = env
    monkeypatch.setattr(remote_esp32, "read_mac", lambda port, stop=None: None)
    done = threading.Event()

    def fake_control_server(cfg, mon, device):
        assert device.state == DeviceState.UNKNOWN
        mon.output = "I (300) app: mac = AABBCCDDEEFF\n"   # el firmware imprime la MAC
        # promote() pone la MAC y después hace la transición: esperar el estado, no la MAC
        if wait_for(lambda: device.state == DeviceState.MONITORING):
            seen["promoted"] = device.state
        wait_for(lambda: (base / "devices.json").exists())   # register_mac va después del promote
        done.set()

    monkeypatch.setattr(remote_esp32, "control_server", fake_control_server)
    threading.Thread(target=lambda: (done.wait(10), remote_esp32._shutdown.set())).start()
    run_main(tty, base)

    assert seen["promoted"] == DeviceState.MONITORING
    log = (base / "devices" / "AABBCCDDEEFF" / "output.log").read_text()
    assert "ESP-IDF v5.3.2" in log                   # lo del provisorio migró al de la MAC
    assert not (base / "devices" / "unknown-ttyUSB3").exists()
    assert MAC in json.loads((base / "devices.json").read_text())


def test_elf_prefers_device_home(env, monkeypatch):
    base, tty, seen = env
    monkeypatch.setattr(remote_esp32, "read_mac", lambda port, stop=None: MAC)
    run_main(tty, base, stop_after=0.3)
    device = seen["device"]
    elf_resolver = FakeMon.instances[0].elf_path
    assert elf_resolver() == base / "current_ttyUSB3.elf"          # todavía no hubo flash con MAC
    (base / "devices" / "AABBCCDDEEFF").mkdir(parents=True, exist_ok=True)
    (base / "devices" / "AABBCCDDEEFF" / "current.elf").write_bytes(b"ELF")
    assert elf_resolver() == base / "devices" / "AABBCCDDEEFF" / "current.elf"


def test_startup_drops_reservation_of_other_board(env, monkeypatch):
    """Los ttyUSB se renumeraron: la reserva de ttyUSB3 era para otra placa."""
    import time
    base, tty, seen = env
    monkeypatch.setattr(remote_esp32, "read_mac", lambda port, stop=None: MAC)
    (base / "locks").mkdir()
    lock = base / "locks" / "ttyUSB3"
    lock.write_text(f"juan:x:{int(time.time()) + 600}:112233445566")
    run_main(tty, base, stop_after=0.3)
    assert not lock.exists()


def test_startup_keeps_reservation_of_same_board(env, monkeypatch):
    import time
    base, tty, seen = env
    monkeypatch.setattr(remote_esp32, "read_mac", lambda port, stop=None: MAC)
    (base / "locks").mkdir()
    lock = base / "locks" / "ttyUSB3"
    lock.write_text(f"juan:x:{int(time.time()) + 600}:AABBCCDDEEFF")
    run_main(tty, base, stop_after=0.3)
    assert lock.exists()


def test_startup_with_unreadable_token_does_not_crash(env, monkeypatch):
    base, tty, seen = env
    monkeypatch.setattr(remote_esp32, "read_mac", lambda port, stop=None: MAC)
    (base / "api_token").mkdir()
    run_main(tty, base, stop_after=0.3)
    log = (base / "devices" / "AABBCCDDEEFF" / "output.log").read_text()
    assert "token=ILEGIBLE" in log


def test_signal_during_discover_exits_without_monitor(env, monkeypatch):
    """Un reinicio de la sesión (SIGTERM/SIGHUP) mientras esptool lee la MAC: el proceso
    sale enseguida, sin pasar a monitoring ni abrir el server. Antes seguía ~10 s en
    discover() (esptool con reintentos) y quien lo reiniciaba lo veía todavía vivo."""
    import time
    base, tty, seen = env
    calls = []

    def slow_read_mac(port, stop=None):
        calls.append(port)
        remote_esp32._on_signal(signal.SIGTERM, None)       # llega en medio de la lectura
        (stop or threading.Event()).wait(2)                 # esptool tarda; con stop, corta
        return None

    monkeypatch.setattr(remote_esp32, "read_mac", slow_read_mac)
    lines = []
    taglog.add_sink(lambda ts, level, tag, msg: lines.append(f"{tag}: {msg}"))
    t0 = time.monotonic()
    run_main(tty, base)
    assert time.monotonic() - t0 < 1.5
    assert len(calls) == 1                                  # sin reintentos
    assert FakeMon.instances == [] and "device" not in seen  # ni monitor ni control server
    assert runstate.read("ttyUSB3") is None
    text = "\n".join(lines)
    assert "señal durante el arranque" in text
    assert "-> unknown" not in text and "-> monitoring" not in text

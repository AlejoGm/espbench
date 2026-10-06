"""
Tests para TtyPort / Device / DeviceManager (FSM), fase 2 del refactor.
Sin hardware — mac_reader se inyecta como fake.
"""
import json
import pathlib
import threading
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server.device import Device, DeviceManager, DeviceState, InvalidTransition, TtyPort
from server.device_log import DeviceLog

MAC = "AA:BB:CC:DD:EE:FF"


def make_device(monkeypatch, tmp_path, tty_path="/dev/ttyUSB0"):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    tty_port = TtyPort.from_tty_path(tty_path)
    return Device(tty_port, DeviceLog(tty_port.tty_name))


# ---------------------------------------------------------------------------
# TtyPort
# ---------------------------------------------------------------------------

def test_ttyport_parses_tcp_port_from_tty_number():
    assert TtyPort.from_tty_path("/dev/ttyUSB0").tcp_port == 5000
    assert TtyPort.from_tty_path("/dev/ttyUSB3").tcp_port == 5003


def test_ttyport_tty_name():
    assert TtyPort.from_tty_path("/dev/ttyUSB0").tty_name == "ttyUSB0"


def test_ttyport_malformed_defaults_to_base_port():
    assert TtyPort.from_tty_path("/dev/ttyACM0").tcp_port == 5000


# ---------------------------------------------------------------------------
# Device — discovery
# ---------------------------------------------------------------------------

def test_starts_in_discovering(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    assert device.state == DeviceState.DISCOVERING
    assert device.mac is None


def test_promote_moves_to_monitoring_and_adopts_log(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.device_log.write_serial(b"boot antes de conocer MAC\n")

    device.promote(MAC)

    assert device.state == DeviceState.MONITORING
    assert device.mac == MAC
    out = tmp_path / "devices" / "AABBCCDDEEFF" / "output.log"
    assert "boot antes de conocer MAC" in out.read_text()


def test_mark_unknown_from_discovering(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.mark_unknown()
    assert device.state == DeviceState.UNKNOWN


def test_promote_from_unknown_resolves_later(monkeypatch, tmp_path):
    """MAC no se leyó al toque (esptool falló), se resuelve despues por serial."""
    device = make_device(monkeypatch, tmp_path)
    device.mark_unknown()
    device.promote(MAC)
    assert device.state == DeviceState.MONITORING
    assert device.mac == MAC


def test_cannot_promote_twice(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.promote(MAC)
    with pytest.raises(InvalidTransition):
        device.promote("11:22:33:44:55:66")


def test_cannot_mark_unknown_after_monitoring(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.promote(MAC)
    with pytest.raises(InvalidTransition):
        device.mark_unknown()


# ---------------------------------------------------------------------------
# Device — flash lifecycle
# ---------------------------------------------------------------------------

def test_flash_lifecycle(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.promote(MAC)

    device.start_flash()
    assert device.state == DeviceState.FLASHING

    device.finish_flash()
    assert device.state == DeviceState.MONITORING


def test_cannot_flash_while_discovering(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    with pytest.raises(InvalidTransition):
        device.start_flash()


def test_cannot_flash_twice(monkeypatch, tmp_path):
    """Guard real: hoy solo el lock file evita flash concurrente. Acá lo rechaza la FSM."""
    device = make_device(monkeypatch, tmp_path)
    device.promote(MAC)
    device.start_flash()
    with pytest.raises(InvalidTransition):
        device.start_flash()


def test_finish_flash_without_start_raises(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.promote(MAC)
    with pytest.raises(InvalidTransition):
        device.finish_flash()


# ---------------------------------------------------------------------------
# Device — erase lifecycle
# ---------------------------------------------------------------------------

def test_erase_lifecycle(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.promote(MAC)

    device.start_erase()
    assert device.state == DeviceState.ERASING

    device.finish_erase()
    assert device.state == DeviceState.MONITORING


def test_cannot_erase_while_flashing(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.promote(MAC)
    device.start_flash()
    with pytest.raises(InvalidTransition):
        device.start_erase()


def test_cannot_flash_while_erasing(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.promote(MAC)
    device.start_erase()
    with pytest.raises(InvalidTransition):
        device.start_flash()


# ---------------------------------------------------------------------------
# Device — disconnect
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("setup", [
    lambda d: None,                                   # DISCOVERING
    lambda d: d.mark_unknown(),                        # UNKNOWN
    lambda d: d.promote(MAC),                          # MONITORING
    lambda d: (d.promote(MAC), d.start_flash()),       # FLASHING
    lambda d: (d.promote(MAC), d.start_erase()),       # ERASING
])
def test_disconnect_allowed_from_any_state(monkeypatch, tmp_path, setup):
    device = make_device(monkeypatch, tmp_path)
    setup(device)
    device.disconnect()
    assert device.state == DeviceState.DISCONNECTED


def test_disconnect_is_idempotent(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.disconnect()
    device.disconnect()  # no debe explotar
    assert device.state == DeviceState.DISCONNECTED


def test_disconnect_closes_device_log(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.promote(MAC)
    device.disconnect()
    assert device.device_log._fh is None


# ---------------------------------------------------------------------------
# DeviceManager
# ---------------------------------------------------------------------------

def test_manager_discover_promotes_when_mac_found(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB0", mac_reader=lambda: MAC)
    manager.discover()
    assert manager.device.state == DeviceState.MONITORING
    assert manager.device.mac == MAC


def test_manager_discover_marks_unknown_when_mac_reader_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB0", mac_reader=lambda: None)
    manager.discover()
    assert manager.device.state == DeviceState.UNKNOWN


def test_manager_wires_tcp_port_from_tty(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB3", mac_reader=lambda: None)
    assert manager.tty_port.tcp_port == 5003
    assert manager.device.tty_name == "ttyUSB3"


def test_manager_accepts_explicit_tcp_port(monkeypatch, tmp_path):
    """El puerto no tiene por qué salir del nombre del tty (ver issue #15 —
    puertos estables por slot USB: el tty podría llamarse esp-slot3 y el
    puerto seguir siendo el que decide la capa de infra, no el nombre)."""
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/esp-slot3", mac_reader=lambda: None, tcp_port=5003)
    assert manager.tty_port.tcp_port == 5003
    assert manager.device.tty_name == "esp-slot3"


# ---------------------------------------------------------------------------
# UNKNOWN también flashea (flash encryption) y la FSM vuelve al estado previo
# ---------------------------------------------------------------------------

def test_unknown_can_flash_and_returns_to_unknown(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.mark_unknown()
    device.start_flash()
    assert device.state == DeviceState.FLASHING
    device.finish_flash()
    assert device.state == DeviceState.UNKNOWN


def test_unknown_can_erase(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.mark_unknown()
    device.start_erase()
    device.finish_erase()
    assert device.state == DeviceState.UNKNOWN


def test_mac_resolved_during_flash_resumes_to_monitoring(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.mark_unknown()
    device.start_flash()
    device.promote(MAC)
    assert device.state == DeviceState.FLASHING     # no interrumpe el flash
    assert device.mac == MAC
    device.finish_flash()
    assert device.state == DeviceState.MONITORING


def test_busy_flag(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.promote(MAC)
    assert not device.busy
    device.start_flash()
    assert device.busy


def test_cannot_flash_while_disconnected(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    device.promote(MAC)
    device.disconnect()
    with pytest.raises(InvalidTransition):
        device.start_flash()


# ---------------------------------------------------------------------------
# Publicación de estado
# ---------------------------------------------------------------------------

def test_every_transition_is_published(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    seen = []
    tty_port = TtyPort(tty_path="/dev/ttyUSB0", tcp_port=5000)
    device = Device(tty_port, DeviceLog("ttyUSB0"), state_sink=seen.append)
    device.promote(MAC)
    device.start_flash()
    device.finish_flash()
    device.disconnect()
    assert [s["state"] for s in seen] == ["discovering", "monitoring", "flashing", "monitoring", "disconnected"]
    last = seen[-1]
    assert last["mac"] == MAC and last["tcp_port"] == 5000 and last["tty"] == "ttyUSB0"
    assert last["log_path"].endswith("AABBCCDDEEFF/output.log")


def test_broken_state_sink_does_not_break_transition(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))

    def boom(snapshot):
        raise OSError("disco lleno")

    device = Device(TtyPort("/dev/ttyUSB0", 5000), DeviceLog("ttyUSB0"), state_sink=boom)
    device.promote(MAC)
    device.start_flash()
    assert device.state == DeviceState.FLASHING


def test_manager_publishes_to_run_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB2", mac_reader=lambda: MAC, tcp_port=5002)
    manager.discover()
    state = json.loads((tmp_path / "run" / "ttyUSB2.json").read_text())
    assert state["state"] == "monitoring" and state["mac"] == MAC and state["tcp_port"] == 5002


# ---------------------------------------------------------------------------
# DeviceManager: reintentos, fallback por serial, watcher de tty
# ---------------------------------------------------------------------------

def test_discover_retries_until_mac(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    answers = iter([None, None, MAC])
    sleeps = []
    manager = DeviceManager("/dev/ttyUSB0", mac_reader=lambda: next(answers), publish_state=False)
    assert manager.discover(attempts=3, delay=3, sleep=sleeps.append) is True
    assert manager.device.state == DeviceState.MONITORING
    assert sleeps == [3, 3]


def test_discover_gives_up_to_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB0", mac_reader=lambda: None, publish_state=False)
    assert manager.discover(attempts=3, delay=0, sleep=lambda s: None) is False
    assert manager.device.state == DeviceState.UNKNOWN


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_resolve_mac_from_output_promotes(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB0", mac_reader=lambda: None, publish_state=False)
    manager.discover()
    outputs = iter(["boot...", "boot... mac = AABBCCDDEEFF"])
    parse = lambda text: MAC if "mac =" in text else None  # noqa: E731
    clock = FakeClock()
    ok = manager.resolve_mac_from_output(lambda: next(outputs), parse,
                                         timeout=15, poll=0.5, clock=clock, sleep=clock.sleep)
    assert ok and manager.device.state == DeviceState.MONITORING and manager.device.mac == MAC


def test_resolve_mac_from_output_times_out(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB0", mac_reader=lambda: None, publish_state=False)
    manager.discover()
    clock = FakeClock()
    ok = manager.resolve_mac_from_output(lambda: "nada", lambda t: None,
                                         timeout=15, poll=0.5, clock=clock, sleep=clock.sleep)
    assert ok is False and manager.device.state == DeviceState.UNKNOWN


def test_watch_tty_disconnects_when_tty_disappears(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB0", mac_reader=lambda: MAC, publish_state=False)
    manager.discover()
    present = iter([True, True, False])
    stop = threading.Event()
    assert manager.watch_tty(stop, exists=lambda p: next(present), poll=0) is True
    assert manager.device.state == DeviceState.DISCONNECTED


def test_watch_tty_stops_on_request(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB0", mac_reader=lambda: MAC, publish_state=False)
    manager.discover()
    stop = threading.Event()
    stop.set()
    assert manager.watch_tty(stop, exists=lambda p: True) is False
    assert manager.device.state == DeviceState.MONITORING


# ---------------------------------------------------------------------------
# Salud del device (SerialWatch) en el estado publicado
# ---------------------------------------------------------------------------

BOOT = b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\nI (31) app_init: App version:      v1.2.3\r\n"
PANIC = b"Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.\r\n"


def test_manager_publishes_health_and_fw_from_serial(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB2", mac_reader=lambda: MAC, tcp_port=5002)
    manager.discover()
    manager.on_serial(BOOT)
    manager.on_serial(PANIC)
    state = json.loads((tmp_path / "run" / "ttyUSB2.json").read_text())
    assert state["fw"]["version"] == "v1.2.3"
    assert state["health"]["boots"] == 1 and state["health"]["panics"] == 1
    assert state["health"]["last_panic"]["detail"] == "LoadProhibited"
    # El serial también llega al log del device
    assert "Guru Meditation" in (tmp_path / "devices" / "AABBCCDDEEFF" / "output.log").read_text()


def test_flash_resets_health_counters_but_keeps_fw(monkeypatch, tmp_path):
    """El flash reinicia el chip a propósito: no tiene que quedar como problema."""
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB0", mac_reader=lambda: MAC, publish_state=False)
    manager.discover()
    manager.on_serial(BOOT + PANIC)
    manager.device.start_flash()
    health = manager.device.snapshot()["health"]
    assert health["boots"] == 0 and health["panics"] == 0 and health["last_panic"] is None
    assert manager.device.snapshot()["fw"]["version"] == "v1.2.3"


def test_device_without_watch_has_no_health(monkeypatch, tmp_path):
    device = make_device(monkeypatch, tmp_path)
    assert "health" not in device.snapshot()


# ---------------------------------------------------------------------------
# Eventos: state (FSM) y los del serial por la tubería del DeviceLog
# ---------------------------------------------------------------------------

def _events(tmp_path, mac_dir="AABBCCDDEEFF"):
    from server import events
    return events.read(tmp_path / "devices" / mac_dir / "events.jsonl")


def test_transitions_are_state_events(monkeypatch, tmp_path):
    from server import taglog
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB0", mac_reader=lambda: MAC, publish_state=False)
    taglog.add_sink(manager.device.device_log.taglog_sink)
    try:
        manager.discover()
        device = manager.device
        device.start_flash()
    finally:
        taglog.reset_default_sinks()
    device.finish_flash()
    device.disconnect()
    states = [(e["detail"]["from"], e["detail"]["to"]) for e in _events(tmp_path) if e["type"] == "state"]
    assert states == [("discovering", "monitoring"), ("monitoring", "flashing"),
                      ("flashing", "monitoring"), ("monitoring", "disconnected")]
    # el cursor apunta a la línea taglog de la transición
    from server.events import parse_cursor
    log = (tmp_path / "devices" / "AABBCCDDEEFF" / "output.log").read_bytes()
    ev = [e for e in _events(tmp_path) if e["type"] == "state"][1]
    _, off = parse_cursor(ev["cursor"])
    assert log[off:].split(b"\n")[0].endswith(b"monitoring -> flashing")


def test_unknown_device_state_events_go_to_provisional_home(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB0", mac_reader=lambda: None, publish_state=False)
    manager.discover()
    evs = _events(tmp_path, "unknown-ttyUSB0")
    assert [e["type"] for e in evs] == ["session", "state"]
    assert evs[1]["detail"] == {"from": "discovering", "to": "unknown"}


def test_serial_events_point_to_their_line(monkeypatch, tmp_path):
    """on_serial → DeviceLog → SerialWatch → events.jsonl: el cursor del boot y
    del panic es el inicio de su línea en output.log."""
    from server.events import parse_cursor
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB0", mac_reader=lambda: MAC, publish_state=False)
    manager.discover()
    data = BOOT + PANIC
    for i in range(0, len(data), 5):
        manager.on_serial(data[i:i + 5])
    log = (tmp_path / "devices" / "AABBCCDDEEFF" / "output.log").read_bytes()
    found = {}
    for e in _events(tmp_path):
        if e["type"] in ("boot", "panic"):
            _, off = parse_cursor(e["cursor"])
            found[e["type"]] = log[off:].split(b"\n")[0].decode()[26:]
    assert found["boot"].startswith("rst:0x1 (POWERON_RESET)")
    assert found["panic"].startswith("Guru Meditation Error")


def test_state_event_cursor_is_atomic_with_its_taglog_line(monkeypatch, tmp_path):
    """Una línea serial de otro hilo que llega justo entre el evento state y su
    línea taglog no puede meterse en el medio: el cursor tiene que caer en
    "a -> b". El sink que dispara la línea intrusa corre antes que el del log."""
    from server import taglog
    from server.events import parse_cursor
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB0", mac_reader=lambda: MAC, publish_state=False)
    log = manager.device.device_log

    def intruder(ts, level, tag, msg):
        if "->" in msg:
            t = threading.Thread(target=manager.on_serial, args=(b"I (1) app: intrusa\r\n",))
            t.start()
            t.join(0.2)          # con el lock tomado por _set_state, espera y entra después

    taglog.clear_sinks()
    taglog.add_sink(intruder)
    taglog.add_sink(log.taglog_sink)
    try:
        manager.discover()
        manager.device.start_flash()
    finally:
        taglog.reset_default_sinks()
    data = (tmp_path / "devices" / "AABBCCDDEEFF" / "output.log").read_bytes()
    for e in _events(tmp_path):
        if e["type"] == "state":
            _, off = parse_cursor(e["cursor"])
            line = data[off:].split(b"\n")[0].decode()
            assert line.endswith(f"{e['detail']['from']} -> {e['detail']['to']}"), line
    assert data.count(b"intrusa") == 2

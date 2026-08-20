"""
Tests para TtyPort / Device / DeviceManager (FSM), fase 2 del refactor.
Sin hardware — mac_reader se inyecta como fake.
"""
import pathlib
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
    device.device_log.write("boot antes de conocer MAC\n")

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

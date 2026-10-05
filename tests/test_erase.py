"""
Tests para erase.py — modo Erase Region interactivo, sin hardware.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server import erase
from server.device import DeviceManager, DeviceState
from server.monitor import EspMonitor

BOOT_LOG = """\
I (67) boot:  0 nvs              WiFi data        01 02 00009000 00006000
I (82) boot:  1 factory          factory app      00 00 00010000 00100000
"""


class FakeMonitor:
    """Solo la interfaz pública de EspMonitor. Si erase.py tocara un privado
    (_stdin_access_lock, _restore_stdin...) esto tira AttributeError."""

    def __init__(self, answers, output=BOOT_LOG):
        self._answers = list(answers)
        self._output = output
        self.calls = []

    def get_recent_output(self):
        return self._output

    def interactive_input(self, prompt):
        self.calls.append("input")
        return self._answers.pop(0)

    def stop(self):
        self.calls.append("stop")

    def start(self):
        self.calls.append("start")


@pytest.fixture
def device(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    manager = DeviceManager("/dev/ttyUSB0", mac_reader=lambda: "AA:BB:CC:DD:EE:FF", publish_state=False)
    manager.discover()
    return manager.device


@pytest.fixture
def esptool(monkeypatch):
    ran = []
    monkeypatch.setattr(erase, "find_esptool_cmd", lambda: ["esptool"])
    monkeypatch.setattr(erase, "run_cmd", lambda cmd, log=None, **kw: ran.append(cmd) or 0)
    return ran


def test_select_partitions():
    parts = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    assert erase.select_partitions(parts, "all") == parts
    assert erase.select_partitions(parts, "1,3") == [parts[0], parts[2]]
    assert erase.select_partitions(parts, "") is None
    assert erase.select_partitions(parts, "x") is None
    assert erase.select_partitions(parts, "9") is None


def test_parse_manual_region():
    assert erase.parse_manual_region("0x9000 0x6000") == [{"offset": 0x9000, "size": 0x6000, "name": "manual"}]
    assert erase.parse_manual_region("0x9000") is None
    assert erase.parse_manual_region("zz 0x10") is None


def test_erase_selected_partition(esptool, device):
    mon = FakeMonitor(answers=["1", "s"])
    erase.erase_region_interactive(mon, {"tty": "/dev/ttyUSB0"}, device)
    assert len(esptool) == 1
    assert esptool[0][-3:] == ["0x9000", "0x6000", "--force"]
    assert mon.calls == ["input", "input", "stop", "start"]
    assert device.state == DeviceState.MONITORING


def test_cancel_at_confirmation_does_not_touch_flash(esptool, device):
    mon = FakeMonitor(answers=["all", "n"])
    erase.erase_region_interactive(mon, {"tty": "/dev/ttyUSB0"}, device)
    assert esptool == []
    assert "stop" not in mon.calls


def test_manual_region_when_no_table(esptool, device):
    mon = FakeMonitor(answers=["0x9000 0x1000", "s"], output="sin tabla")
    erase.erase_region_interactive(mon, {"tty": "/dev/ttyUSB0"}, device)
    assert esptool[0][-3:] == ["0x9000", "0x1000", "--force"]


def test_manual_cancel(esptool, device):
    mon = FakeMonitor(answers=["cancel"], output="sin tabla")
    erase.erase_region_interactive(mon, {"tty": "/dev/ttyUSB0"}, device)
    assert esptool == [] and mon.calls == ["input"]


def test_monitor_restarts_even_if_esptool_fails(monkeypatch, device):
    monkeypatch.setattr(erase, "find_esptool_cmd", lambda: ["esptool"])

    def boom(cmd, log=None, **kw):
        raise RuntimeError("puerto ocupado")

    monkeypatch.setattr(erase, "run_cmd", boom)
    mon = FakeMonitor(answers=["1", "s"])
    erase.erase_region_interactive(mon, {"tty": "/dev/ttyUSB0"}, device)
    assert mon.calls[-2:] == ["stop", "start"]
    assert device.state == DeviceState.MONITORING


def test_espmonitor_interactive_input(monkeypatch, tmp_path):
    mon = EspMonitor("/dev/ttyUSB0", 115200)
    monkeypatch.setattr("builtins.input", lambda: "  0x9000 0x6000  ")
    assert mon.interactive_input("prompt: ") == "0x9000 0x6000"


def test_espmonitor_interactive_input_without_tty(monkeypatch, tmp_path):
    mon = EspMonitor("/dev/ttyUSB0", 115200)

    def eof():
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert mon.interactive_input("prompt: ") == ""


def test_erase_is_busy_state_during_esptool(monkeypatch, device):
    seen = []
    monkeypatch.setattr(erase, "find_esptool_cmd", lambda: ["esptool"])
    monkeypatch.setattr(erase, "run_cmd", lambda cmd, log=None, **kw: seen.append(device.state) or 0)
    erase.erase_region_interactive(FakeMonitor(answers=["1", "s"]), {"tty": "/dev/ttyUSB0"}, device)
    assert seen == [DeviceState.ERASING] and device.state == DeviceState.MONITORING


def test_erase_rejected_while_flashing(esptool, device):
    device.start_flash()
    mon = FakeMonitor(answers=["1", "s"])
    erase.erase_region_interactive(mon, {"tty": "/dev/ttyUSB0"}, device)
    assert esptool == [] and "stop" not in mon.calls
    assert device.state == DeviceState.FLASHING


def test_espmonitor_output_goes_to_sink_buffer_and_stdout(capsysbinary):
    got = []
    mon = EspMonitor("/dev/ttyUSB0", 115200, output_sink=got.append)
    mon._on_output(b"I (67) boot: hola\n")
    assert got == [b"I (67) boot: hola\n"]
    assert "hola" in mon.get_recent_output()
    assert capsysbinary.readouterr().out == b"I (67) boot: hola\r\n"


def test_espmonitor_resolves_elf_on_each_start(tmp_path):
    elf = tmp_path / "current.elf"
    mon = EspMonitor("/dev/ttyUSB0", 115200, elf_path=lambda: elf)
    assert mon._resolve_elf() is None       # todavía no hubo flash
    elf.write_bytes(b"ELF")
    assert mon._resolve_elf() == elf

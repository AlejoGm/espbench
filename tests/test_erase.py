"""
Tests para erase.py — modo Erase Region interactivo, sin hardware.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server import erase
from server.monitor import EspMonitor, _ignore_signals_flag

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
def esptool(monkeypatch):
    ran = []
    monkeypatch.setattr(erase, "find_esptool_cmd", lambda: ["esptool"])
    monkeypatch.setattr(erase, "run_cmd", lambda cmd, log, **kw: ran.append(cmd) or 0)
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


def test_erase_selected_partition(esptool):
    mon = FakeMonitor(answers=["1", "s"])
    erase.erase_region_interactive(mon, {"tty": "/dev/ttyUSB0"}, svc_log=None)
    assert len(esptool) == 1
    assert esptool[0][-3:] == ["0x9000", "0x6000", "--force"]
    assert mon.calls == ["input", "input", "stop", "start"]
    assert not _ignore_signals_flag.is_set()


def test_cancel_at_confirmation_does_not_touch_flash(esptool):
    mon = FakeMonitor(answers=["all", "n"])
    erase.erase_region_interactive(mon, {"tty": "/dev/ttyUSB0"}, svc_log=None)
    assert esptool == []
    assert "stop" not in mon.calls


def test_manual_region_when_no_table(esptool):
    mon = FakeMonitor(answers=["0x9000 0x1000", "s"], output="sin tabla")
    erase.erase_region_interactive(mon, {"tty": "/dev/ttyUSB0"}, svc_log=None)
    assert esptool[0][-3:] == ["0x9000", "0x1000", "--force"]


def test_manual_cancel(esptool):
    mon = FakeMonitor(answers=["cancel"], output="sin tabla")
    erase.erase_region_interactive(mon, {"tty": "/dev/ttyUSB0"}, svc_log=None)
    assert esptool == [] and mon.calls == ["input"]


def test_monitor_restarts_even_if_esptool_fails(monkeypatch):
    monkeypatch.setattr(erase, "find_esptool_cmd", lambda: ["esptool"])

    def boom(cmd, log, **kw):
        raise RuntimeError("puerto ocupado")

    monkeypatch.setattr(erase, "run_cmd", boom)
    mon = FakeMonitor(answers=["1", "s"])
    erase.erase_region_interactive(mon, {"tty": "/dev/ttyUSB0"}, svc_log=None)
    assert mon.calls[-2:] == ["stop", "start"]
    assert not _ignore_signals_flag.is_set()


def test_espmonitor_interactive_input(monkeypatch, tmp_path):
    mon = EspMonitor("/dev/ttyUSB0", 115200, tmp_path)
    monkeypatch.setattr("builtins.input", lambda: "  0x9000 0x6000  ")
    assert mon.interactive_input("prompt: ") == "0x9000 0x6000"


def test_espmonitor_interactive_input_without_tty(monkeypatch, tmp_path):
    mon = EspMonitor("/dev/ttyUSB0", 115200, tmp_path)

    def eof():
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert mon.interactive_input("prompt: ") == ""

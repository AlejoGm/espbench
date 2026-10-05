"""
Tests para runstate: estado runtime por tty en run/<tty>.json.
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server import runstate


def test_write_then_read(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    runstate.write("ttyUSB0", {"state": "monitoring", "mac": "AA"})
    assert runstate.read("ttyUSB0") == {"state": "monitoring", "mac": "AA"}


def test_read_missing_is_none(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    assert runstate.read("ttyUSB9") is None


def test_read_corrupt_is_none(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "ttyUSB0.json").write_text('{"state": "monit')
    assert runstate.read("ttyUSB0") is None


def test_write_leaves_no_temp_files(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    for i in range(5):
        runstate.write("ttyUSB0", {"n": i})
    assert [f.name for f in (tmp_path / "run").iterdir()] == ["ttyUSB0.json"]
    assert runstate.read("ttyUSB0") == {"n": 4}


def test_list_all(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    runstate.write("ttyUSB0", {"state": "monitoring"})
    runstate.write("esp-slot3", {"state": "flashing"})
    assert set(runstate.list_all()) == {"ttyUSB0", "esp-slot3"}


def test_list_all_without_run_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    assert runstate.list_all() == {}


def test_pid_alive():
    assert runstate.pid_alive(os.getpid())
    assert not runstate.pid_alive(None)
    assert not runstate.pid_alive(2 ** 22 + 12345)


def test_remove(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    runstate.write("ttyUSB0", {})
    runstate.remove("ttyUSB0")
    runstate.remove("ttyUSB0")   # idempotente
    assert runstate.read("ttyUSB0") is None

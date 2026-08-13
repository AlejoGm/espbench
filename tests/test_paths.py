"""
Tests para paths.py: fuente única de rutas generadas en runtime bajo ESP_BASE.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server import paths


def test_default_base_when_env_unset(monkeypatch):
    monkeypatch.delenv("ESP_BASE", raising=False)
    assert paths.esp_base() == pathlib.Path("/opt/esp")


def test_base_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    assert paths.esp_base() == tmp_path


def test_top_level_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    assert paths.logs_dir() == tmp_path / "logs"
    assert paths.jobs_dir() == tmp_path / "jobs"
    assert paths.locks_dir() == tmp_path / "locks"
    assert paths.devices_dir() == tmp_path / "devices"
    assert paths.devices_file() == tmp_path / "devices.json"
    assert paths.version_file() == tmp_path / "VERSION"


def test_tty_scoped_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    assert paths.tty_log_dir("ttyUSB0") == tmp_path / "logs" / "ttyUSB0"
    assert paths.mac_file("ttyUSB0") == tmp_path / "logs" / "ttyUSB0" / "mac"
    assert paths.last_user_file("ttyUSB0") == tmp_path / "logs" / "ttyUSB0" / "last_user"
    assert paths.lock_file("ttyUSB0") == tmp_path / "locks" / "ttyUSB0"
    assert paths.current_elf_file("ttyUSB0") == tmp_path / "current_ttyUSB0.elf"


def test_tty_scoped_paths_isolated_by_tty_name(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    assert paths.mac_file("ttyUSB0") != paths.mac_file("ttyUSB1")


def test_device_home_normalizes_mac(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    expected = tmp_path / "devices" / "AABBCCDDEEFF"
    assert paths.device_home("AA:BB:CC:DD:EE:FF") == expected
    assert paths.device_home("aa:bb:cc:dd:ee:ff") == expected
    assert paths.device_home("AA-BB-CC-DD-EE-FF") == expected
    assert paths.device_home("AABBCCDDEEFF") == expected


def test_device_scoped_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    mac = "AA:BB:CC:DD:EE:FF"
    home = tmp_path / "devices" / "AABBCCDDEEFF"
    assert paths.device_output_log(mac) == home / "output.log"
    assert paths.device_current_elf(mac) == home / "current.elf"
    assert paths.device_jobs_dir(mac) == home / "jobs"
    assert paths.device_state_file(mac) == home / "state.json"

"""
Tests para DeviceLog: buffer hasta conocer la MAC, después escritor único
del log del device.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server.device_log import DeviceLog
from server import paths

MAC = "AA:BB:CC:DD:EE:FF"


def _out_path(tmp_path):
    return tmp_path / "devices" / "AABBCCDDEEFF" / "output.log"


def test_buffers_before_adopt(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0")
    log.write("linea 1\n")
    log.write("linea 2\n")
    assert not log.adopted
    assert not _out_path(tmp_path).exists()


def test_adopt_flushes_buffer(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0")
    log.write("linea 1\n")
    log.write("linea 2\n")
    log.adopt(MAC)
    log.close()

    content = _out_path(tmp_path).read_text()
    assert "linea 1" in content
    assert "linea 2" in content
    assert "adoptado desde tty=ttyUSB0" in content
    assert log.adopted


def test_write_after_adopt_goes_direct_to_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0")
    log.adopt(MAC)
    log.write("linea post-adopt\n")
    log.close()

    assert "linea post-adopt" in _out_path(tmp_path).read_text()


def test_double_adopt_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0")
    log.adopt(MAC)
    log.adopt("11:22:33:44:55:66")
    log.write("x\n")
    log.close()

    assert _out_path(tmp_path).exists()
    assert not (tmp_path / "devices" / "112233445566").exists()


def test_appends_across_instances_same_mac(monkeypatch, tmp_path):
    """Reconexión del mismo device (nueva instancia) no pisa el historial."""
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log1 = DeviceLog("ttyUSB0")
    log1.adopt(MAC)
    log1.write("sesion 1\n")
    log1.close()

    log2 = DeviceLog("ttyUSB1")  # mismo device, reconectado en otro puerto
    log2.adopt(MAC)
    log2.write("sesion 2\n")
    log2.close()

    content = _out_path(tmp_path).read_text()
    assert "sesion 1" in content
    assert "sesion 2" in content


def test_write_without_adopt_never_touches_disk(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0")
    for i in range(50):
        log.write(f"linea {i}\n")
    assert not paths.devices_dir().exists()

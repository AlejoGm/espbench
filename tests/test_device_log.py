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


def test_new_session_rotates_previous(monkeypatch, tmp_path):
    """Reconexión del mismo device: la sesión anterior rota a output_<ts>.log
    (lo que antes hacía esp32_tmux.sh con mv), output.log queda con la nueva."""
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log1 = DeviceLog("ttyUSB0")
    log1.adopt(MAC)
    log1.write("sesion 1\n")
    log1.close()

    log2 = DeviceLog("ttyUSB1")  # mismo device, reconectado en otro puerto
    log2.adopt(MAC)
    log2.write("sesion 2\n")
    log2.close()

    home = tmp_path / "devices" / "AABBCCDDEEFF"
    assert "sesion 2" in (home / "output.log").read_text()
    assert "sesion 1" not in (home / "output.log").read_text()
    rotated = list(home.glob("output_*.log"))
    assert len(rotated) == 1 and "sesion 1" in rotated[0].read_text()


def test_rotation_skips_empty_previous(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    DeviceLog("ttyUSB0").adopt(MAC)          # sesión vacía
    DeviceLog("ttyUSB0").adopt(MAC)
    assert list((tmp_path / "devices" / "AABBCCDDEEFF").glob("output_*.log")) == []


def test_write_without_adopt_never_touches_disk(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0")
    for i in range(50):
        log.write(f"linea {i}\n")
    assert not paths.devices_dir().exists()


def test_path_is_none_until_adopted(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0")
    assert log.path is None
    log.adopt(MAC)
    assert log.path == _out_path(tmp_path)


def test_buffer_is_capped(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0", buffer_limit=100)
    for i in range(50):
        log.write(f"linea {i:02d}\n")       # 9 chars c/u -> 450 > 100
    log.adopt(MAC)
    log.close()
    content = _out_path(tmp_path).read_text()
    assert "linea 49" in content
    assert "linea 00" not in content
    assert "descartados" in content


def test_write_bytes_handles_split_utf8(monkeypatch, tmp_path):
    """Un carácter UTF-8 partido entre dos lecturas del PTY no se rompe."""
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0")
    log.adopt(MAC)
    data = "temperatura 25°C\n".encode()
    cut = data.index("°".encode()) + 1          # en el medio del °
    log.write_bytes(data[:cut])
    log.write_bytes(data[cut:])
    log.close()
    assert "25°C" in _out_path(tmp_path).read_text()


def test_unknown_goes_to_provisional_home(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0")
    log.write("boot sin mac\n")
    log.adopt_unknown()
    log.write("sigue\n")
    log.close()
    content = (tmp_path / "devices" / "unknown-ttyUSB0" / "output.log").read_text()
    assert "boot sin mac" in content and "sigue" in content


def test_late_mac_migrates_provisional_log(monkeypatch, tmp_path):
    """MAC resuelta tarde por serial: lo que ya se escribió pasa al archivo de
    la MAC y el provisorio desaparece."""
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0")
    log.adopt_unknown()
    log.write("antes de la mac\n")
    log.adopt(MAC)
    log.write("despues\n")
    log.close()
    content = _out_path(tmp_path).read_text()
    assert "antes de la mac" in content and "despues" in content
    assert "MAC resuelta" in content
    assert not (tmp_path / "devices" / "unknown-ttyUSB0").exists()


def test_taglog_sink_writes_formatted_line(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0")
    log.adopt(MAC)
    log.taglog_sink("2026-10-05 12:00:00", "INFO", "protocol", "flash ok")
    log.close()
    line = _out_path(tmp_path).read_text().strip().splitlines()[-1]
    assert "INFO" in line and "protocol" in line and "flash ok" in line


def test_taglog_sink_skips_debug(monkeypatch, tmp_path):
    """DEBUG (progreso de upload, dump de flasher_args...) no ensucia el log del dashboard."""
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0")
    log.adopt(MAC)
    log.taglog_sink("2026-10-05 12:00:00", "DEBUG", "protocol", "progreso: 1/2 bytes")
    log.taglog_sink("2026-10-05 12:00:00", "WARN", "protocol", "aviso")
    log.close()
    content = _out_path(tmp_path).read_text()
    assert "progreso" not in content and "aviso" in content

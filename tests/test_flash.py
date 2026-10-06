import pytest, pathlib, tempfile, json, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))
from server.flash import build_esptool_cmd, parse_mac_from_serial

FAKE_ESPTOOL = ["python", "-m", "esptool"]

def make_jobdir_with_flasher_args(flash_files_data):
    tmp = pathlib.Path(tempfile.mkdtemp())
    fa = {"flash_files": flash_files_data}
    (tmp / "flasher_args.json").write_text(json.dumps(fa))
    # create fake bin files
    for name in ["bootloader.bin", "partition-table.bin", "ota_data_initial.bin", "app.bin"]:
        (tmp / name).write_bytes(b"fake")
    return tmp

def test_build_cmd_dict():
    d = make_jobdir_with_flasher_args({"0x1000": "bootloader.bin", "0x10000": "app.bin"})
    _, write_cmd, pairs = build_esptool_cmd(FAKE_ESPTOOL, "esp32", "/dev/ttyUSB0", 921600, False, False, d)
    offsets = [p[0] for p in pairs]
    assert "0x1000" in offsets and "0x10000" in offsets

def test_build_cmd_list():
    d = make_jobdir_with_flasher_args([["0x1000", "bootloader.bin"], ["0x10000", "app.bin"]])
    _, write_cmd, pairs = build_esptool_cmd(FAKE_ESPTOOL, "esp32", "/dev/ttyUSB0", 921600, False, False, d)
    assert len(pairs) >= 2

def test_build_cmd_fallback():
    # empty flash_files → fallback by filename
    d = make_jobdir_with_flasher_args({})
    _, write_cmd, pairs = build_esptool_cmd(FAKE_ESPTOOL, "esp32", "/dev/ttyUSB0", 921600, False, False, d)
    assert len(pairs) >= 1

def test_build_cmd_encrypt_flag():
    d = make_jobdir_with_flasher_args({"0x1000": "bootloader.bin", "0x10000": "app.bin"})
    _, write_cmd, pairs = build_esptool_cmd(FAKE_ESPTOOL, "esp32", "/dev/ttyUSB0", 921600, True, False, d)
    assert "--encrypt" in write_cmd

def test_build_cmd_erase():
    d = make_jobdir_with_flasher_args({"0x1000": "bootloader.bin", "0x10000": "app.bin"})
    erase_cmd, _, _ = build_esptool_cmd(FAKE_ESPTOOL, "esp32", "/dev/ttyUSB0", 921600, False, True, d)
    assert erase_cmd is not None and "erase-flash" in erase_cmd


# --- parse_mac_from_serial ---

REAL_BOOT_LINE = (
    "I (10) DeviceIdentity ./lib/sfy-Device/src/DeviceIdentity.cpp:34 "
    "init(): serial = 185030827029496 mac = F8B3B7D848A8"
)

def test_parse_mac_real_boot_line():
    assert parse_mac_from_serial(REAL_BOOT_LINE) == "F8:B3:B7:D8:48:A8"

def test_parse_mac_uppercase():
    assert parse_mac_from_serial("mac = AABBCCDDEEFF") == "AA:BB:CC:DD:EE:FF"

def test_parse_mac_lowercase():
    assert parse_mac_from_serial("mac = aabbccddeeff") == "AA:BB:CC:DD:EE:FF"

def test_parse_mac_with_ansi_wrapped_line():
    # ANSI codes wrapping the whole line (not the MAC value)
    line = "\x1b[0;32mI (10) DeviceIdentity init(): serial = 123 mac = F8B3B7D848A8\x1b[0m\r\n"
    assert parse_mac_from_serial(line) == "F8:B3:B7:D8:48:A8"

def test_parse_mac_with_ansi_embedded_in_value():
    # esp_idf_monitor colorizes the MAC value itself — ANSI code between '= ' and hex
    line = "I (10) DeviceIdentity init(): serial = \x1b[96m185030827029496\x1b[0m mac = \x1b[96mF8B3B7D848A8\x1b[0m"
    assert parse_mac_from_serial(line) == "F8:B3:B7:D8:48:A8"

def test_parse_mac_crlf():
    line = "mac = F8B3B7D848A8\r\n"
    assert parse_mac_from_serial(line) == "F8:B3:B7:D8:48:A8"

def test_parse_mac_not_found():
    assert parse_mac_from_serial("nothing useful here") is None

def test_parse_mac_too_short():
    assert parse_mac_from_serial("mac = F8B3B7D848") is None  # 10 chars

def test_parse_mac_no_false_positive_sha256():
    line = "I (1340) app_init: ELF file SHA256:  5b6b0da6098ac865..."
    assert parse_mac_from_serial(line) is None


def test_run_cmd_lines_reach_taglog_job_log_and_callback():
    import logging
    from server import taglog
    from server.flash import run_cmd

    seen = []
    taglog.reset_default_sinks()
    taglog.add_sink(lambda ts, lvl, tag, msg: seen.append((tag, msg)))
    job = logging.getLogger("test.job"); job.setLevel(logging.INFO)
    records = []
    h = logging.Handler(); h.emit = lambda r: records.append(r.getMessage()); job.addHandler(h)
    streamed = []
    try:
        rc = run_cmd([sys.executable, "-c", "print('Writing at 0x10000'); print('Hash ok')"],
                     job, on_line=streamed.append)
    finally:
        taglog.reset_default_sinks()
        job.removeHandler(h)
    assert rc == 0
    assert ("esptool", "Writing at 0x10000") in seen and ("esptool", "Hash ok") in seen
    assert streamed == ["Writing at 0x10000", "Hash ok"]
    assert "Writing at 0x10000" in records and "EXIT 0" in records


def test_run_cmd_without_job_log():
    from server.flash import run_cmd
    assert run_cmd([sys.executable, "-c", "import sys; sys.exit(3)"]) == 3


def _fake_esptool(monkeypatch, script):
    from server import flash
    monkeypatch.setattr(flash, "find_esptool_cmd", lambda: [sys.executable, "-c", script])
    return flash


def test_read_mac_parses_esptool_output(monkeypatch):
    flash = _fake_esptool(monkeypatch, "print('Chip is ESP32'); print('MAC: aa:bb:cc:dd:ee:ff')")
    assert flash.read_mac("/dev/ttyUSB0") == "AA:BB:CC:DD:EE:FF"


def test_read_mac_stop_terminates_esptool(monkeypatch, tmp_path):
    """Una señal al proceso mientras esptool lee (stop): esptool se termina enseguida,
    no queda vivo con el puerto abierto."""
    import threading
    import time
    pidfile = tmp_path / "pid"
    flash = _fake_esptool(monkeypatch, f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); "
                                       "time.sleep(30)")
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    t0 = time.monotonic()
    assert flash.read_mac("/dev/ttyUSB0", stop=stop) is None
    assert time.monotonic() - t0 < 3
    import os
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_read_mac_timeout_kills_esptool(monkeypatch):
    import time
    flash = _fake_esptool(monkeypatch, "import time; time.sleep(30)")
    t0 = time.monotonic()
    assert flash.read_mac("/dev/ttyUSB0", timeout=0.5) is None
    assert time.monotonic() - t0 < 3

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

def test_parse_mac_with_ansi():
    line = "\x1b[0;32mI (10) DeviceIdentity init(): serial = 123 mac = F8B3B7D848A8\x1b[0m\r\n"
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

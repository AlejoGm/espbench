"""
Tests para parse_partition_table: tabla de particiones del bootloader ESP32.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server.partition_table import parse_partition_table

BOOT_LOG = """\
I (29) boot: ESP-IDF v5.3.2 2nd stage bootloader
I (56) boot: Partition Table:
I (60) boot: ## Label            Usage          Type ST Offset   Length
I (67) boot:  0 nvs              WiFi data        01 02 00009000 00006000
I (75) boot:  1 phy_init         RF data          01 01 0000f000 00001000
I (82) boot:  2 factory          factory app      00 00 00010000 00100000
I (90) boot: End of partition table
I (94) esp_image: segment 0: paddr=00010020 vaddr=3f400020
"""


def test_parses_bootloader_table():
    parts = parse_partition_table(BOOT_LOG)
    assert [p["name"] for p in parts] == ["nvs", "phy_init", "factory"]
    nvs = parts[0]
    assert nvs["offset"] == 0x9000 and nvs["size"] == 0x6000
    assert nvs["type"] == "01" and nvs["subtype"] == "02"
    assert nvs["offset_hex"] == "0x00009000"
    assert parts[2]["offset"] == 0x10000 and parts[2]["size"] == 0x100000


def test_parses_ansi_colored_lines():
    colored = "\n".join(f"\x1b[0;32m{line}\x1b[0m" for line in BOOT_LOG.splitlines())
    assert [p["name"] for p in parse_partition_table(colored)] == ["nvs", "phy_init", "factory"]


def test_ignores_non_table_output():
    assert parse_partition_table("I (94) esp_image: segment 0\nhola mundo\n") == []


def test_empty_text():
    assert parse_partition_table("") == []


def test_table_buried_in_long_log():
    noise = "W (1234) wifi: algo\n" * 200
    assert len(parse_partition_table(noise + BOOT_LOG + noise)) == 3

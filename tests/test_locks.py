"""Tests de locks.py: formato user:token[:expires[:mac]], vencimiento (se borra
al leer) y la reserva de otra placa al arrancar."""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server import locks, paths

FUTURE = int(time.time()) + 3600
PAST = int(time.time()) - 10


def test_parse_flash_lock_compatible():
    assert locks.parse("alejo:t0k") == locks.Lock("alejo", "t0k")
    assert locks.parse("alejo") == locks.Lock("alejo", "")
    assert locks.parse("") is None


def test_parse_and_format_reservation():
    lock = locks.parse(f"alejo:t0k:{FUTURE}:aabbccddeeff")
    assert lock == locks.Lock("alejo", "t0k", FUTURE, "AABBCCDDEEFF") and lock.reservation
    assert locks.format_lock(lock) == f"alejo:t0k:{FUTURE}:AABBCCDDEEFF"
    assert locks.format_lock(locks.Lock("a", "b", FUTURE)) == f"a:b:{FUTURE}"
    assert locks.format_lock(locks.Lock("a", "b", None, "AABBCCDDEEFF")) == "a:b"


def test_read_deletes_expired():
    locks.write("ttyUSB0", locks.Lock("alejo", "t0k", PAST))
    assert locks.read("ttyUSB0") is None
    assert not paths.lock_file("ttyUSB0").exists()


def test_read_keeps_valid_and_permanent():
    locks.write("ttyUSB0", locks.Lock("alejo", "t0k", FUTURE, "AA:BB:CC:DD:EE:FF"))
    locks.write("ttyUSB1", locks.Lock("juan", "x"))
    assert locks.read("ttyUSB0").mac == "AABBCCDDEEFF"
    assert locks.read("ttyUSB1") == locks.Lock("juan", "x")
    assert paths.lock_file("ttyUSB0").stat().st_mode & 0o777 == 0o666


def test_drop_if_other_board():
    locks.write("ttyUSB0", locks.Lock("alejo", "t0k", FUTURE, "AABBCCDDEEFF"))
    assert locks.drop_if_other_board("ttyUSB0", "AA:BB:CC:DD:EE:FF") is None   # la misma placa
    assert paths.lock_file("ttyUSB0").exists()
    assert locks.drop_if_other_board("ttyUSB0", "11:22:33:44:55:66").user == "alejo"
    assert not paths.lock_file("ttyUSB0").exists()


def test_drop_if_other_board_keeps_flash_lock_and_reservation_without_mac():
    locks.write("ttyUSB0", locks.Lock("alejo", "t0k"))
    locks.write("ttyUSB1", locks.Lock("alejo", "t0k", FUTURE))
    assert locks.drop_if_other_board("ttyUSB0", "11:22:33:44:55:66") is None
    assert locks.drop_if_other_board("ttyUSB1", "11:22:33:44:55:66") is None
    assert paths.lock_file("ttyUSB0").exists() and paths.lock_file("ttyUSB1").exists()

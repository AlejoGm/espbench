"""
Tests para SerialWatch: salud del firmware a partir del serial, con logs con
el formato real de la ROM y del IDF.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server.serial_watch import SerialWatch

BOOT = (
    "ets Jun  8 2016 00:22:57\r\n\r\n"
    "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n"
    "configsip: 0, SPIWP:0xee\r\n"
    "\x1b[0;32mI (29) boot: ESP-IDF v5.3.2 2nd stage bootloader\x1b[0m\r\n"
    "\x1b[0;32mI (313) app_init: Project name:     SFY1-56_1\x1b[0m\r\n"
    "\x1b[0;32mI (318) app_init: App version:      1.2.3\x1b[0m\r\n"
    "\x1b[0;32mI (334) app_init: ESP-IDF:          v5.3.2\x1b[0m\r\n"
)
PANIC = (
    "Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled.\r\n"
    "Backtrace: 0x400d1234:0x3ffb1234 0x400d5678:0x3ffb5678\r\n"
    "Rebooting...\r\n"
    "ets Jun  8 2016 00:22:57\r\n"
    "rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n"
)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_boot_reads_reset_and_firmware():
    w = SerialWatch()
    w.feed(BOOT.encode())
    assert w.boots == 1
    assert w.last_reset["reason"] == "POWERON_RESET" and not w.last_reset["abnormal"]
    assert w.firmware() == {"project": "SFY1-56_1", "version": "1.2.3", "idf": "v5.3.2"}
    assert w.panics == 0 and not w.health()["boot_loop"]


def test_panic_is_detected_with_kind_and_detail():
    w = SerialWatch()
    w.feed((BOOT + PANIC).encode())
    h = w.health()
    assert h["panics"] == 1 and h["boots"] == 2
    assert h["last_panic"]["kind"] == "panic" and h["last_panic"]["detail"] == "LoadProhibited"
    assert "Guru Meditation" in h["last_panic"]["line"]
    assert h["last_reset"]["reason"] == "SW_CPU_RESET"


def test_other_panic_kinds():
    for text, kind in [
        ("abort() was called at PC 0x400d1234 on core 0", "abort"),
        ("Brownout detector was triggered", "brownout"),
        ("E (5000) task_wdt: Task watchdog got triggered.", "task_wdt"),
        ("***ERROR*** A stack overflow in task mqtt_task has been detected.", "stack_overflow"),
        ("assert failed: xQueueGenericSend queue.c:832", "assert"),
    ]:
        w = SerialWatch()
        w.feed((text + "\r\n").encode())
        assert w.last_panic["kind"] == kind, text


def test_watchdog_and_brownout_resets_are_abnormal():
    for reason in ("TG0WDT_SYS_RESET", "RTCWDT_BROWN_OUT_RESET", "TG1WDT_SYS_RST"):
        w = SerialWatch()
        w.feed(f"rst:0x7 ({reason}),boot:0x13\r\n".encode())
        assert w.last_reset["abnormal"], reason


def test_boot_loop_within_window_and_expiry():
    clock = Clock()
    w = SerialWatch(clock=clock)
    for _ in range(3):
        w.feed(b"rst:0xc (SW_CPU_RESET),boot:0x13\r\n")
        clock.t += 10
    assert w.boot_loop
    clock.t += 300                      # el device se estabilizó
    assert not w.boot_loop


def test_lines_split_across_chunks_and_utf8():
    w = SerialWatch()
    data = BOOT.encode()
    for i in range(0, len(data), 7):    # el PTY entrega pedazos arbitrarios
        w.feed(data[i:i + 7])
    assert w.firmware()["project"] == "SFY1-56_1" and w.boots == 1


def test_on_change_only_when_something_changes():
    calls = []
    w = SerialWatch(on_change=lambda: calls.append(1))
    w.feed(b"I (100) app: nada importante\r\n")
    assert calls == []
    w.feed(BOOT.encode())
    assert len(calls) == 1               # un feed con varios cambios = una notificación
    w.feed(BOOT.replace("rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n", "").encode())
    assert len(calls) == 1               # misma versión de firmware: no hay cambio


def test_new_firmware_version_after_flash_is_picked_up():
    w = SerialWatch()
    w.feed(BOOT.encode())
    w.feed(BOOT.replace("1.2.3", "1.2.4").encode())
    assert w.firmware()["version"] == "1.2.4"


def test_garbage_without_newline_does_not_grow_forever():
    w = SerialWatch()
    w.feed(b"x" * 10000)
    assert len(w._partial) <= 4096

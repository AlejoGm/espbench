"""
Tests para SerialWatch: salud del firmware y eventos a partir de las líneas
del serial, con logs con el formato real de la ROM y del IDF.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server.device_log import DeviceLog
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
RST = "rst:0xc (SW_CPU_RESET),boot:0x13\r\n"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def feed(w, text, start=0):
    """Como DeviceLog: una llamada por línea terminada en \\n, con un cursor por
    línea (acá, el número de línea)."""
    for i, line in enumerate(text.split("\n")[:-1]):
        w.on_line(line, f"c:20261005_160000_1:{start + i}", 1000.0 + start + i)


def watch_with_events(**kw):
    evs = []
    w = SerialWatch(on_event=lambda t, d, c, ts: evs.append((t, d, c, ts)), **kw)
    return w, evs


# ---------------------------------------------------------------------------
# Salud y firmware (los de siempre, por líneas)
# ---------------------------------------------------------------------------

def test_boot_reads_reset_and_firmware():
    w = SerialWatch()
    feed(w, BOOT)
    assert w.boots == 1
    assert w.last_reset["reason"] == "POWERON_RESET" and not w.last_reset["abnormal"]
    assert w.firmware() == {"project": "SFY1-56_1", "version": "1.2.3", "idf": "v5.3.2"}
    assert w.panics == 0 and not w.health()["boot_loop"]


def test_panic_is_detected_with_kind_and_detail():
    w = SerialWatch()
    feed(w, BOOT + PANIC)
    h = w.health()
    assert h["panics"] == 1 and h["boots"] == 2
    assert h["last_panic"]["kind"] == "guru" and h["last_panic"]["detail"] == "LoadProhibited"
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
        feed(w, text + "\r\n")
        assert w.last_panic["kind"] == kind, text


def test_watchdog_and_brownout_resets_are_abnormal():
    for reason in ("TG0WDT_SYS_RESET", "RTCWDT_BROWN_OUT_RESET", "TG1WDT_SYS_RST"):
        w = SerialWatch()
        feed(w, f"rst:0x7 ({reason}),boot:0x13\r\n")
        assert w.last_reset["abnormal"], reason


def test_boot_loop_within_window_and_expiry():
    clock = Clock()
    w = SerialWatch(clock=clock)
    for _ in range(3):
        feed(w, RST)
        clock.t += 10
    assert w.boot_loop
    clock.t += 300                      # el device se estabilizó
    assert not w.boot_loop


def test_carriage_return_keeps_last_segment_and_strips_ansi():
    """La regla del \\r ahora la aplica SerialWatch (DeviceLog corta solo en \\n)."""
    w = SerialWatch()
    w.on_line("basura 10%\r\x1b[0;32mI (318) app_init: App version:      9.9.9\x1b[0m\r")
    assert w.firmware()["version"] == "9.9.9"


def test_lines_split_across_chunks_and_utf8_through_devicelog(monkeypatch, tmp_path):
    """Integración con la tubería real: el PTY entrega pedazos arbitrarios y
    DeviceLog arma las líneas que recibe SerialWatch."""
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0", autoflush=False)
    w = SerialWatch()
    log.line_sink = w.on_line
    data = ("I (1) app: ñandú °C\r\n" + BOOT).encode()
    for i in range(0, len(data), 7):
        log.write_serial(data[i:i + 7])
    assert w.firmware()["project"] == "SFY1-56_1" and w.boots == 1


def test_on_change_only_when_something_changes():
    calls = []
    w = SerialWatch(on_change=lambda: calls.append(1))
    feed(w, "I (100) app: nada importante\r\n")
    assert calls == []
    feed(w, BOOT)
    assert len(calls) == 4               # rst + proyecto + versión + IDF
    feed(w, BOOT.replace("rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n", ""))
    assert len(calls) == 4               # misma versión de firmware: no hay cambio


def test_new_firmware_version_after_flash_is_picked_up():
    w = SerialWatch()
    feed(w, BOOT)
    feed(w, BOOT.replace("1.2.3", "1.2.4"))
    assert w.firmware()["version"] == "1.2.4"


def test_garbage_line_is_ignored():
    w = SerialWatch()
    w.on_line("x" * 10000)
    assert w.boots == 0 and w.panics == 0 and w.firmware() == {}


# ---------------------------------------------------------------------------
# Eventos
# ---------------------------------------------------------------------------

def test_boot_event_on_every_rst_with_cursor_and_ts():
    w, evs = watch_with_events()
    feed(w, BOOT)
    feed(w, RST, start=100)
    boots = [e for e in evs if e[0] == "boot"]
    assert [b[1] for b in boots] == [{"reason": "POWERON_RESET", "abnormal": False},
                                     {"reason": "SW_CPU_RESET", "abnormal": False}]
    assert boots[0][2] == "c:20261005_160000_1:2" and boots[0][3] == 1002.0   # la línea del rst:
    assert boots[1][2] == "c:20261005_160000_1:100"


def test_panic_event():
    w, evs = watch_with_events()
    feed(w, PANIC)
    (panic,) = [e for e in evs if e[0] == "panic"]
    assert panic[1]["kind"] == "guru" and panic[1]["reason"] == "LoadProhibited"
    assert panic[1]["line"].startswith("Guru Meditation Error")
    assert panic[2] == "c:20261005_160000_1:0"


def test_fw_event_only_when_it_changes():
    w, evs = watch_with_events()
    feed(w, BOOT)
    feed(w, BOOT, start=100)                              # mismo firmware
    feed(w, BOOT.replace("1.2.3", "1.2.4"), start=200)    # flash nuevo
    fws = [e for e in evs if e[0] == "fw"]
    assert [f[1]["version"] for f in fws] == ["1.2.3", "1.2.4"]
    assert fws[0][1] == {"project": "SFY1-56_1", "version": "1.2.3", "idf": "v5.3.2"}
    assert fws[0][2] == "c:20261005_160000_1:5"           # primera línea que cambió (Project name)
    assert fws[1][2] == "c:20261005_160000_1:206"         # App version del segundo flash


def test_fw_event_without_idf_line_goes_out_at_next_boot():
    w, evs = watch_with_events()
    feed(w, "I (318) app_init: App version:      3.0\r\n")
    assert [e[0] for e in evs] == []
    feed(w, RST, start=10)
    assert [e[0] for e in evs] == ["fw", "boot"]


def test_boot_loop_groups_boots_into_start_and_end():
    """Mientras dura el boot loop no se registran boots sueltos."""
    clock = Clock()
    w, evs = watch_with_events(clock=clock)
    for i in range(10):
        feed(w, RST, start=i)
        clock.t += 5
    types = [e[0] for e in evs]
    assert types == ["boot", "boot", "boot_loop"]
    assert evs[2][1] == {"phase": "start", "boots": 3}
    clock.t += 300                                        # se estabilizó
    feed(w, "I (100) app: andando\r\n", start=50)
    assert evs[-1][0] == "boot_loop"
    assert evs[-1][1]["phase"] == "end" and evs[-1][1]["boots"] == 10
    assert evs[-1][2] == "c:20261005_160000_1:50"
    feed(w, RST, start=60)
    assert evs[-1][0] == "boot"                           # vuelve a registrar boots


def test_flash_ends_an_active_boot_loop():
    clock = Clock()
    w, evs = watch_with_events(clock=clock)
    for i in range(3):
        feed(w, RST, start=i)
    w.reset_counters()
    assert evs[-1][0] == "boot_loop" and evs[-1][1]["phase"] == "end" and evs[-1][2] is None
    feed(w, RST, start=10)
    assert evs[-1][0] == "boot"


def test_boot_loop_end_has_real_end_time_and_last_boot():
    """El end no lleva la hora en que se notó, sino último boot + ventana."""
    from server import events
    clock = Clock()
    w, evs = watch_with_events(clock=clock, boot_loop_window=120)
    for i in range(4):
        feed(w, RST, start=i)
        if i < 3:
            clock.t += 5
    last_boot_t = clock.t
    clock.t += 600                                        # diez minutos después llega una línea
    feed(w, "I (100) app: andando\r\n", start=50)
    end = evs[-1]
    assert end[0] == "boot_loop" and end[1]["phase"] == "end"
    assert end[3] == last_boot_t + 120
    assert end[1]["last_boot"] == {"ts": events.iso_ms(1003.0), "cursor": "c:20261005_160000_1:3"}


def test_poll_closes_boot_loop_of_a_silent_board():
    clock = Clock()
    w, evs = watch_with_events(clock=clock)
    for i in range(3):
        feed(w, RST, start=i)
    assert not w.poll()                                   # sigue en loop
    clock.t += 300                                        # la placa quedó muda
    assert w.poll()
    assert evs[-1][0] == "boot_loop" and evs[-1][1]["phase"] == "end" and evs[-1][2] is None
    assert not w.poll()                                   # una sola vez


def test_event_errors_are_logged_not_swallowed():
    from server import taglog
    seen = []
    taglog.add_sink(lambda ts, level, tag, msg: seen.append((level, msg)))
    try:
        w = SerialWatch(on_event=lambda *a: (_ for _ in ()).throw(OSError("disco lleno")))
        feed(w, RST)
    finally:
        taglog.reset_default_sinks()
    assert any(level == "DEBUG" and "disco lleno" in msg for level, msg in seen)

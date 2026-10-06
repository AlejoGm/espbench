"""
Tests para DeviceLog: buffer hasta conocer la MAC, después escritor único
del log del device. Tubería de líneas con prefijo (hora + origen), línea
serial parcial retenida, sesión, header y cursores en bytes.
"""
import pathlib
import re
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server.device_log import DeviceLog, PREFIX_RE, parse_cursor, read_session_id
from server import paths

MAC = "AA:BB:CC:DD:EE:FF"
T0 = 1_791_212_523.1235  # 2026-10-05 16:02:03.123 en UTC-3 (los asserts no dependen de la zona)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _out_path(tmp_path):
    return tmp_path / "devices" / "AABBCCDDEEFF" / "output.log"


def _lines(path):
    return path.read_bytes().decode("utf-8").split("\n")[:-1]


def _bodies(path):
    """(origen, cuerpo) de cada línea, sin la hora. La primera es el header."""
    out = []
    for line in _lines(path):
        assert PREFIX_RE.match(line), f"línea sin prefijo: {line!r}"
        out.append((line[24], line[26:]))
    return out


def make_log(monkeypatch, tmp_path, **kw):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    kw.setdefault("autoflush", False)
    kw.setdefault("clock", Clock())
    return DeviceLog("ttyUSB0", **kw)


# ---------------------------------------------------------------------------
# Buffer, adopción y rotación (comportamiento de siempre)
# ---------------------------------------------------------------------------

def test_buffers_before_adopt(monkeypatch, tmp_path):
    log = make_log(monkeypatch, tmp_path)
    log.write_serial(b"linea 1\n")
    log.write_taglog("INFO", "x", "linea 2")
    assert not log.adopted
    assert not _out_path(tmp_path).exists()


def test_adopt_writes_header_then_buffer_then_marker(monkeypatch, tmp_path):
    log = make_log(monkeypatch, tmp_path)
    log.write_serial(b"linea 1\n")
    log.write_serial(b"linea 2\n")
    log.adopt(MAC)
    log.close()

    bodies = _bodies(_out_path(tmp_path))
    assert bodies[0][0] == "|" and f"sesión {log.session_id} tty=ttyUSB0" in bodies[0][1]
    assert bodies[1:3] == [(">", "linea 1"), (">", "linea 2")]
    assert "adoptado desde tty=ttyUSB0" in bodies[3][1]      # después del buffer
    assert log.adopted


def test_write_after_adopt_goes_direct_to_file(monkeypatch, tmp_path):
    log = make_log(monkeypatch, tmp_path)
    log.adopt(MAC)
    log.write_serial(b"linea post-adopt\n")
    assert (">", "linea post-adopt") in _bodies(_out_path(tmp_path))
    log.close()


def test_double_adopt_is_noop(monkeypatch, tmp_path):
    log = make_log(monkeypatch, tmp_path)
    log.adopt(MAC)
    log.adopt("11:22:33:44:55:66")
    log.write_serial(b"x\n")
    log.close()

    assert _out_path(tmp_path).exists()
    assert not (tmp_path / "devices" / "112233445566").exists()


def test_new_session_rotates_previous_by_session_id(monkeypatch, tmp_path):
    """Reconexión del mismo device: la sesión anterior rota a
    output_<session_id>.log, con el id leído de su header."""
    log1 = make_log(monkeypatch, tmp_path)
    log1.adopt(MAC)
    log1.write_serial(b"sesion 1\n")
    log1.close()

    log2 = make_log(monkeypatch, tmp_path, clock=Clock(T0 + 3600))   # otra sesión, una hora después
    log2.adopt(MAC)
    log2.write_serial(b"sesion 2\n")
    log2.close()

    home = tmp_path / "devices" / "AABBCCDDEEFF"
    assert "sesion 2" in (home / "output.log").read_text()
    assert "sesion 1" not in (home / "output.log").read_text()
    rotated = list(home.glob("output_*.log"))
    assert [p.name for p in rotated] == [f"output_{log1.session_id}.log"]
    assert "sesion 1" in rotated[0].read_text()
    assert read_session_id(home / "output.log") == log2.session_id


def test_rotation_without_header_uses_current_time(monkeypatch, tmp_path):
    """Log viejo (sin header): output_<hora actual>.log, como antes."""
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    old = _out_path(tmp_path)
    old.parent.mkdir(parents=True)
    old.write_text("log viejo sin prefijo\n")
    DeviceLog("ttyUSB0", autoflush=False).adopt(MAC)
    rotated = list(old.parent.glob("output_*.log"))
    assert len(rotated) == 1 and re.fullmatch(r"output_\d{8}_\d{6}\.log", rotated[0].name)
    assert rotated[0].read_text() == "log viejo sin prefijo\n"


def test_rotation_skips_empty_previous(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    _out_path(tmp_path).parent.mkdir(parents=True)
    _out_path(tmp_path).touch()
    DeviceLog("ttyUSB0", autoflush=False).adopt(MAC)
    assert list((tmp_path / "devices" / "AABBCCDDEEFF").glob("output_*.log")) == []


def test_write_without_adopt_never_touches_disk(monkeypatch, tmp_path):
    log = make_log(monkeypatch, tmp_path)
    for i in range(50):
        log.write_serial(f"linea {i}\n".encode())
    assert not paths.devices_dir().exists()


def test_path_is_none_until_adopted(monkeypatch, tmp_path):
    log = make_log(monkeypatch, tmp_path)
    assert log.path is None and log.end_cursor() is None
    log.adopt(MAC)
    assert log.path == _out_path(tmp_path)


def test_buffer_is_capped_but_header_survives(monkeypatch, tmp_path):
    """El buffer descarta desde el principio; el header no está en el buffer."""
    log = make_log(monkeypatch, tmp_path, buffer_limit=100)
    for i in range(50):
        log.write_serial(f"linea {i:02d}\n".encode())     # ~35 bytes c/u con prefijo
    log.adopt(MAC)
    log.close()
    content = _out_path(tmp_path).read_text()
    assert "linea 49" in content
    assert "linea 00" not in content
    assert "descartados" in content
    assert "sesión " in content.splitlines()[0]


def test_unknown_goes_to_provisional_home(monkeypatch, tmp_path):
    log = make_log(monkeypatch, tmp_path)
    log.write_serial(b"boot sin mac\n")
    log.adopt_unknown()
    log.write_serial(b"sigue\n")
    log.close()
    content = (tmp_path / "devices" / "unknown-ttyUSB0" / "output.log").read_text()
    assert "boot sin mac" in content and "sigue" in content


def test_late_mac_migrates_provisional_log_keeping_offsets(monkeypatch, tmp_path):
    """MAC resuelta tarde por serial: lo que ya se escribió pasa al principio del
    archivo de la MAC (los cursores siguen valiendo) y el provisorio desaparece."""
    log = make_log(monkeypatch, tmp_path)
    log.adopt_unknown()
    log.write_serial(b"antes de la mac\n")
    cursor = log.end_cursor()
    log.adopt(MAC)
    log.write_serial(b"despues\n")
    log.close()
    data = _out_path(tmp_path).read_bytes()
    _, offset = parse_cursor(cursor)
    assert data[:offset].decode().endswith("antes de la mac\n")
    assert data[offset:].decode().splitlines()[0].endswith("--- MAC resuelta: AA:BB:CC:DD:EE:FF ---")
    assert "despues" in data.decode()
    assert not (tmp_path / "devices" / "unknown-ttyUSB0").exists()


# ---------------------------------------------------------------------------
# Prefijo y tubería de líneas
# ---------------------------------------------------------------------------

def test_prefix_has_ms_timestamp_and_origin(monkeypatch, tmp_path):
    log = make_log(monkeypatch, tmp_path)
    log.adopt(MAC)
    log.write_serial(b"I (120) app_init: hola\r\n")
    log.taglog_sink("2026-10-05 16:02:03", "WARN", "protocol", "flash ok")
    lines = _lines(_out_path(tmp_path))
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.123 > I \(120\) app_init: hola", lines[1])
    # taglog: sin "|" doble, el del origen ya está
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.123 \| WARN  \| protocol       \| flash ok", lines[2])
    log.close()


def test_serial_split_in_chunks_and_utf8(monkeypatch, tmp_path):
    """Un carácter UTF-8 partido entre dos lecturas no se rompe; los chunks
    arbitrarios del PTY se juntan en una línea."""
    log = make_log(monkeypatch, tmp_path)
    log.adopt(MAC)
    data = "temperatura 25°C\r\nsegunda\n".encode()
    for i in range(len(data)):
        log.write_serial(data[i:i + 1])
    assert _bodies(_out_path(tmp_path))[1:] == [(">", "temperatura 25°C"), (">", "segunda")]
    log.close()


def test_carriage_returns_inside_line_are_kept(monkeypatch, tmp_path):
    """Solo se corta en \\n. Los \\r internos (barras de progreso) quedan: el
    dashboard y SerialWatch se quedan con el último segmento."""
    log = make_log(monkeypatch, tmp_path)
    log.adopt(MAC)
    log.write_serial(b"10%\r50%\r100%\r\n")
    assert _bodies(_out_path(tmp_path))[1] == (">", "10%\r50%\r100%")
    log.close()


def test_blank_serial_lines_are_kept(monkeypatch, tmp_path):
    log = make_log(monkeypatch, tmp_path)
    log.adopt(MAC)
    log.write_serial(b"a\r\n\r\nb\n")
    assert _bodies(_out_path(tmp_path))[1:] == [(">", "a"), (">", ""), (">", "b")]
    log.close()


def test_partial_line_is_held_until_newline(monkeypatch, tmp_path):
    log = make_log(monkeypatch, tmp_path)
    log.adopt(MAC)
    log.write_serial(b"I (1) app: mitad")
    assert len(_lines(_out_path(tmp_path))) == 1          # solo el header
    log.write_serial(b" y fin\n")
    assert _bodies(_out_path(tmp_path))[1] == (">", "I (1) app: mitad y fin")
    log.close()


def test_partial_line_flushed_after_timeout_then_continuation(monkeypatch, tmp_path):
    """Prompt de esp_console sin \\n: sale a los 150 ms; lo que sigue de esa
    línea sale con ↪."""
    mono = Clock(100.0)
    log = make_log(monkeypatch, tmp_path, monotonic=mono)
    log.adopt(MAC)
    log.write_serial(b"esp> ")
    mono.t += 0.1
    log.tick()
    assert len(_lines(_out_path(tmp_path))) == 1          # todavía no
    mono.t += 0.06
    log.tick()
    assert _bodies(_out_path(tmp_path))[1] == (">", "esp> ")
    log.write_serial(b"help\r\n")
    assert _bodies(_out_path(tmp_path))[2] == ("↪", "help")
    log.write_serial(b"siguiente\n")
    assert _bodies(_out_path(tmp_path))[3] == (">", "siguiente")
    log.close()


def test_partial_flushed_by_background_thread(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0", partial_timeout=0.05)
    log.adopt(MAC)
    log.write_serial(b"esp> ")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and len(_lines(_out_path(tmp_path))) < 2:
        time.sleep(0.01)
    assert _bodies(_out_path(tmp_path))[1] == (">", "esp> ")
    log.close()


def test_taglog_in_the_middle_of_a_partial_serial_line(monkeypatch, tmp_path):
    """Una línea taglog de otro hilo no queda pegada a la serial a medio llegar."""
    log = make_log(monkeypatch, tmp_path)
    log.adopt(MAC)
    log.write_serial(b"I (5) app: medio")
    log.write_taglog("INFO", "protocol", "en el medio")
    log.write_serial(b" fin\n")
    bodies = _bodies(_out_path(tmp_path))
    assert bodies[1] == ("|", "INFO  | protocol       | en el medio")
    assert bodies[2] == (">", "I (5) app: medio fin")
    log.close()


def test_multiline_taglog_gets_prefix_per_line(monkeypatch, tmp_path):
    log = make_log(monkeypatch, tmp_path)
    log.adopt(MAC)
    log.write_taglog("ERROR", "flash", "uno\ndos")
    bodies = _bodies(_out_path(tmp_path))
    assert bodies[1:] == [("|", "ERROR | flash          | uno"), ("|", "ERROR | flash          | dos")]
    log.close()


def test_taglog_sink_skips_debug(monkeypatch, tmp_path):
    """DEBUG (progreso de upload, dump de flasher_args...) no ensucia el log del dashboard."""
    log = make_log(monkeypatch, tmp_path)
    log.adopt(MAC)
    log.taglog_sink("2026-10-05 12:00:00", "DEBUG", "protocol", "progreso: 1/2 bytes")
    log.taglog_sink("2026-10-05 12:00:00", "WARN", "protocol", "aviso")
    log.close()
    content = _out_path(tmp_path).read_text()
    assert "progreso" not in content and "aviso" in content


def test_endless_line_is_cut(monkeypatch, tmp_path):
    """Basura sin \\n (baudrate equivocado): no se acumula para siempre."""
    log = make_log(monkeypatch, tmp_path)
    log.adopt(MAC)
    log.write_serial(b"x" * 10000)
    origins = [o for o, _ in _bodies(_out_path(tmp_path))[1:]]
    assert origins == [">", "↪"]
    assert len(log._pend or "") <= 4096
    log.close()


def test_close_writes_pending_partial(monkeypatch, tmp_path):
    log = make_log(monkeypatch, tmp_path)
    log.adopt(MAC)
    log.write_serial(b"sin fin")
    log.close()
    assert _bodies(_out_path(tmp_path))[-1] == (">", "sin fin")


# ---------------------------------------------------------------------------
# Cursores y offsets en bytes
# ---------------------------------------------------------------------------

def test_session_id_format(monkeypatch, tmp_path):
    log = make_log(monkeypatch, tmp_path)
    assert re.fullmatch(r"\d{8}_\d{6}_\d+", log.session_id)


def test_line_sink_gets_cursor_at_start_of_line_in_bytes(monkeypatch, tmp_path):
    """El cursor es el offset en bytes donde empieza la línea (= fin de la
    anterior), aunque haya caracteres multibyte antes."""
    log = make_log(monkeypatch, tmp_path)
    seen = []
    log.line_sink = lambda text, cursor, ts: seen.append((text, cursor, ts))
    log.adopt(MAC)
    log.write_serial("año ñandú °C\r\n".encode())
    log.write_taglog("INFO", "x", "ü")
    log.write_serial(b"rst:0xc (SW_CPU_RESET)\r\n")
    data = _out_path(tmp_path).read_bytes()
    assert [t for t, _, _ in seen] == ["año ñandú °C", "rst:0xc (SW_CPU_RESET)"]
    for text, cursor, ts in seen:
        session, offset = parse_cursor(cursor)
        assert session == log.session_id
        assert data[offset - 1:offset] == b"\n"                     # cae en fin de línea
        assert data[offset:].split(b"\n")[0].decode().endswith(text)
        assert ts == T0
    assert log.end_cursor() == f"c:{log.session_id}:{len(data)}"
    log.close()


def test_line_sink_gets_whole_logical_line_across_continuation(monkeypatch, tmp_path):
    mono = Clock(0.0)
    log = make_log(monkeypatch, tmp_path, monotonic=mono)
    seen = []
    log.line_sink = lambda text, cursor, ts: seen.append((text, cursor))
    log.adopt(MAC)
    log.write_serial(b"rst:0xc (SW_CPU")
    mono.t += 1
    log.tick()
    log.write_serial(b"_RESET),boot:0x13\r\n")
    assert seen[0][0] == "rst:0xc (SW_CPU_RESET),boot:0x13"
    data = _out_path(tmp_path).read_bytes()
    _, offset = parse_cursor(seen[0][1])
    assert data[offset:].split(b"\n")[0].endswith(b"> rst:0xc (SW_CPU")
    log.close()


def test_stable_offsets_with_premac_buffer(monkeypatch, tmp_path):
    """Las líneas vistas antes de la MAC reciben una posición del buffer que,
    al volcarlo, se traduce al offset real (+ header)."""
    log = make_log(monkeypatch, tmp_path)
    seen = []
    log.line_sink = lambda text, cursor, ts: seen.append((text, cursor))
    log.write_serial(b"uno\n")
    log.write_serial(b"dos\n")
    log.adopt(MAC)
    log.close()
    data = _out_path(tmp_path).read_bytes()
    for text, pos in seen:
        offset = log._buf_to_offset(pos)
        assert data[offset - 1:offset] == b"\n"
        assert data[offset:].split(b"\n")[0].decode().endswith("> " + text)


def test_concurrent_serial_and_taglog_never_mix(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    log = DeviceLog("ttyUSB0", partial_timeout=0.01)
    log.adopt(MAC)
    serial = b"".join(f"I ({i}) app: linea serial numero {i}\r\n".encode() for i in range(300))

    def feed():
        for i in range(0, len(serial), 37):
            log.write_serial(serial[i:i + 37])

    def tags():
        for i in range(300):
            log.write_taglog("INFO", "otro", f"taglog {i}")

    threads = [threading.Thread(target=feed), threading.Thread(target=tags)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.close()
    bodies = _bodies(_out_path(tmp_path))
    tag_lines = [b for o, b in bodies if o == "|" and "taglog" in b]
    assert len(tag_lines) == 300
    # reconstruyendo segmentos + continuaciones salen las 300 líneas serie intactas
    serial_lines, cur = [], None
    for o, b in bodies[1:]:
        if o == ">":
            if cur is not None:
                serial_lines.append(cur)
            cur = b
        elif o == "↪":
            cur += b
    serial_lines.append(cur)
    assert serial_lines == [f"I ({i}) app: linea serial numero {i}" for i in range(300)]

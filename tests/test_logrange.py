"""
Tests de logrange.py: anchors (now, session, eventos con ordinal, tiempo,
cursores), --until (evento, boot/panic en las líneas, patrón sobre la línea
lógica, eco), --around, filtros, truncado y la respuesta de §7.3.

El log se arma a mano con el formato de DeviceLog (prefijo + header de sesión),
así cada test controla horas y offsets. Uno al final usa el DeviceManager real.
"""
import datetime as dt
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server import events, logrange, paths
from server.logrange import RangeError

MAC = "AA:BB:CC:DD:EE:FF"
SID = "20261005_160000_42"
OLD = "20261005_150000_41"
DAY = "2026-10-05"


def epoch(hms: str, day: str = DAY) -> float:
    return dt.datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S.%f").timestamp()


class Board:
    """Arma devices/<MAC>/output.log línea por línea y guarda el offset de cada una."""

    def __init__(self, sid=SID, name="output.log"):
        self.home = paths.device_home(MAC)
        self.home.mkdir(parents=True, exist_ok=True)
        self.sid = sid
        self.path = self.home / name
        self.path.write_bytes(b"")
        self.off = {}
        self.add("16:00:00.000", "|", f"INFO  | devicelog      | sesión {sid} tty=ttyUSB0", key="header")

    def add(self, hms, origin, body, key=None, day=DAY):
        offset = self.path.stat().st_size
        line = body if origin is None else f"{day} {hms} {origin} {body}"
        with open(self.path, "ab") as f:
            f.write((line + "\n").encode())
        if key:
            self.off[key] = offset
        return offset

    def cursor(self, key):
        return events.format_cursor(self.sid, self.off[key] if isinstance(key, str) else key)

    def event(self, type_, key, ts="2026-10-05T16:00:00.000", **detail):
        ev = {"ts": ts, "type": type_, "cursor": self.cursor(key), "detail": detail, "by": "device"}
        with open(self.home / "events.jsonl", "a") as f:
            f.write(json.dumps(ev) + "\n")

    @property
    def size(self):
        return self.path.stat().st_size


def rr(**kw):
    kw.setdefault("now", epoch("16:10:00.000"))
    return logrange.read_range(paths.device_home(MAC), **kw)


def bodies(resp):
    return [l.split(" ", 2)[2] if l[:2].isdigit() else l for l in resp["lines"]]


@pytest.fixture
def b():
    """Sesión típica: boot, app, panic, boot, app."""
    b = Board()
    b.add("16:00:01.000", ">", "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)", key="boot0")
    b.add("16:00:01.100", ">", "\x1b[0;32mI (313) app_init: App version:      v2.4.1\x1b[0m", key="app0")
    b.add("16:00:02.000", "|", "INFO  | protocol       | algo del server", key="tl0")
    b.add("16:00:03.000", ">", "Guru Meditation Error: Core  1 panic'ed (LoadProhibited).", key="panic0")
    b.add("16:00:03.010", ">", "Backtrace: 0x400d1234:0x3ffb1234", key="bt0")
    b.add("16:00:03.020", ">", "rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)", key="boot1")
    b.add("16:00:03.500", ">", "I (400) sensor: temperatura 25.3°C", key="app1")
    b.event("boot", "boot0", reason="POWERON_RESET")
    b.event("panic", "panic0", kind="guru")
    b.event("boot", "boot1", reason="SW_CPU_RESET")
    return b


# ---------- anchors ----------

def test_session_and_now(b):
    r = rr()                                   # since default = session
    assert r["start"] == f"c:{SID}:0" and r["end"] == f"c:{SID}:{b.size}"
    assert len(r["lines"]) == 8 and r["until_found"] is None and not r["truncated"]
    assert r["date"] == DAY and r["lines"][1].startswith("16:00:01.000 > rst:0x1")
    assert rr(since="now")["lines"] == [] and rr(since="now")["start"] == f"c:{SID}:{b.size}"


def test_response_shape_and_server_time(b):
    r = rr(since="panic")
    assert set(r) == {"date", "lines", "start", "end", "until_found", "match", "match_cursor", "echo_seen",
                      "partial", "truncated", "session_ended", "events", "server_time"}
    assert r["partial"] is None and r["session_ended"] is False
    assert dt.datetime.fromisoformat(r["server_time"]).utcoffset() is not None   # con zona
    assert r["server_time"].startswith("2026-10-05T16:10:00.000")


def test_match_cursor_is_the_start_of_the_matched_logical_line(b):
    """match_cursor: inicio de la línea (lógica) del match. --verify lo compara
    con el cursor del boot_loop (C1); en un patrón partido, es el `>`."""
    assert rr(until="boot")["match_cursor"] == b.cursor("boot0")
    assert rr(since=b.cursor("app0"), until="boot")["match_cursor"] == b.cursor("boot1")
    b.add("16:00:04.000", ">", "esp> ", key="prompt")
    b.add("16:00:04.100", "↪", "status OK", key="cont")
    assert rr(since=b.cursor("app1"), until="status OK")["match_cursor"] == b.cursor("prompt")
    b.event("send", "prompt")
    assert rr(since=b.cursor("app1"), until="send")["match_cursor"] == b.cursor("prompt")
    assert rr(since=b.cursor("app1"), until="nunca")["match_cursor"] is None


def test_event_anchors_with_ordinals_sorted_by_offset(b):
    # el api escribe en paralelo: un evento con cursor anterior puede quedar después en el archivo
    b.event("send", "app1")
    b.event("send", "app0")
    assert rr(since="boot")["start"] == b.cursor("boot1")
    assert rr(since="boot~1")["start"] == b.cursor("boot0")
    assert rr(since="send~0")["start"] == b.cursor("app1")
    assert rr(since="send~1")["start"] == b.cursor("app0")
    with pytest.raises(RangeError) as e:
        rr(since="boot~2")
    assert e.value.error == "bad_anchor"


def test_event_anchor_ignores_other_sessions(b):
    ev = {"ts": "x", "type": "flash", "cursor": f"c:{OLD}:10", "detail": {}}
    with open(b.home / "events.jsonl", "a") as f:
        f.write(json.dumps(ev) + "\n")
    with pytest.raises(RangeError) as e:
        rr(since="flash")
    assert e.value.error == "bad_anchor"


@pytest.mark.parametrize("anchor", ["ayer", "boot~x", "c:123", "c:20261005_160000_42:abc", "25:00", "now~1"])
def test_bad_anchors(b, anchor):
    with pytest.raises(RangeError) as e:
        rr(since=anchor)
    assert e.value.error == "bad_anchor"


def test_cursor_mid_line_is_aligned_to_line_start(b):
    r = rr(since=b.cursor(b.off["panic0"] + 7))
    assert r["start"] == b.cursor("panic0") and bodies(r)[0].startswith("Guru Meditation")


def test_cursor_beyond_log_is_bad_anchor(b):
    with pytest.raises(RangeError) as e:
        rr(since=b.cursor(b.size + 100))
    assert e.value.error == "bad_anchor"


def test_cursor_of_rotated_session_and_expired(b):
    old = Board(sid=OLD, name=f"output_{OLD}.log")
    old.add("15:00:01.000", ">", "linea vieja", key="x")
    r = rr(since=old.cursor("x"))
    assert bodies(r) == ["linea vieja"] and r["session_ended"] is True     # no es la sesión actual
    with pytest.raises(RangeError) as e:
        rr(since="c:20261001_000000_1:0")
    assert e.value.error == "cursor_expired"


def test_board_without_log_is_not_found():
    paths.device_home(MAC).mkdir(parents=True)
    with pytest.raises(RangeError) as e:
        rr()
    assert e.value.error == "not_found"


# ---------- tiempo ----------

def time_board():
    b = Board()
    b.add("16:00:10.000", ">", "a", key="a")
    b.add(None, None, "sin prefijo (log viejo)", key="np")
    # serial escrita al completarse (MAX_HOLD): su hora es la del primer byte
    b.add("16:05:00.900", ">", "b", key="b")
    b.add("16:05:00.200", "|", "INFO  | x              | taglog anterior en hora", key="t")
    b.add("16:05:01.500", ">", "c", key="c")
    return b


def test_duration_finds_first_line_at_or_after_despite_disorder():
    b = time_board()
    now = epoch("16:10:00.500")
    assert rr(since="5m", now=now)["start"] == b.cursor("b")         # T = 16:05:00.500
    assert rr(since="300s", now=now)["start"] == b.cursor("b")
    assert rr(since="4m59s".replace("4m59s", "299s"), now=now)["start"] == b.cursor("c")
    assert rr(since="1h", now=now)["start"] == b.cursor("header")    # antes de la sesión: su inicio
    assert rr(since="1s", now=now)["start"] == f"c:{SID}:{b.size}"   # nada tan nuevo: now


def test_clock_and_iso_anchors():
    b = time_board()
    now = epoch("16:10:00.000")
    assert rr(since="16:05:01", now=now)["start"] == b.cursor("c")
    assert rr(since="16:05:01.5", now=now)["start"] == b.cursor("c")
    assert rr(since="2026-10-05T16:00:05", now=now)["start"] == b.cursor("a")
    assert rr(since="2026-10-05 16:05", now=now)["start"] == b.cursor("b")


def test_clock_later_than_now_is_yesterday():
    t = logrange.parse_time("23:50", epoch("00:10:00.000", "2026-10-06"))
    assert dt.datetime.fromtimestamp(t) == dt.datetime(2026, 10, 5, 23, 50)


# ---------- until ----------

def test_until_event_type(b):
    b.event("flash", "app0")
    r = rr(since="boot~1", until="flash")
    assert r["until_found"] is True and r["end"] == b.cursor("tl0")      # fin de la línea del evento
    assert r["match"].startswith("16:00:01.100 > I (313) app_init")


def test_until_boot_and_panic_are_found_in_lines_without_events():
    """Un poll que ve la línea antes que el evento: igual la encuentra."""
    b = Board()
    b.add("16:00:01.000", ">", "hola", key="x")
    b.add("16:00:02.000", ">", "abort() was called at PC 0x40081234", key="p")
    b.add("16:00:02.100", ">", "rst:0xc (SW_CPU_RESET),boot:0x13", key="r")
    assert not (b.home / "events.jsonl").exists()
    assert rr(since="session", until="panic")["end"] == b.cursor("r")
    r = rr(since="session", until="boot")
    assert r["until_found"] and r["match"] == "16:00:02.100 > rst:0xc (SW_CPU_RESET),boot:0x13"


def test_until_same_type_as_since_is_the_next_one(b):
    r = rr(since="boot~1", until="boot")
    assert r["until_found"] and r["match"].startswith("16:00:03.020 > rst:0xc")


def test_until_not_found_then_found_from_end(b):
    r = rr(since="boot", until="re:READY")
    assert r["until_found"] is False and r["end"] == f"c:{SID}:{b.size}" and r["match"] is None
    b.add("16:00:09.000", ">", "app: READY", key="ready")
    r2 = rr(since=r["end"], until="re:READY")
    assert r2["until_found"] and bodies(r2) == ["app: READY"]


def test_until_event_written_after_the_log_snapshot_is_seen_next_poll(b):
    """flash/state/send apuntan a la próxima línea: un evento que todavía no
    estaba tiene cursor >= el end devuelto."""
    r = rr(since="boot", until="flash")
    assert r["until_found"] is False
    b.event("flash", b.size)                         # cursor = fin del log (próxima línea)
    r2 = rr(since=r["end"], until="flash")
    assert r2["until_found"] and r2["end"] == r["end"] and r2["lines"] == [] and r2["match"] is None
    assert [e["type"] for e in r2["events"]] == ["flash"]


def test_pattern_on_clean_logical_line():
    b = Board()
    b.add("16:00:01.000", ">", "esp> ", key="prompt")                     # prompt sin \n (150 ms)
    b.add("16:00:01.200", "|", "INFO  | protocol       | otro hilo", key="tl")
    b.add("16:00:01.300", "↪", "status\r", key="cont")
    b.add("16:00:01.400", ">", "\x1b[0;32mI (9) app: progreso 10%\r50%\r100% listo\x1b[0m", key="pr")
    r = rr(since="session", until="esp> status")                         # unido > + ↪
    assert r["until_found"] and r["end"] == b.cursor("pr")
    assert r["match"] == "16:00:01.000 > esp> status"                   # la línea lógica entera
    r = rr(since="session", until="re:^100% listo$")         # sin ANSI y con el \r aplicado (pisa la línea)
    assert r["until_found"] and r["end"] == f"c:{SID}:{b.size}"
    assert rr(since="session", until="re:progreso")["until_found"] is False   # lo pisó el \r
    assert rr(since="session", until="otro hilo")["until_found"] is True      # taglog también
    assert rr(since="session", until="2026-10-05")["until_found"] is False    # el prefijo no cuenta


def test_echo_does_not_count_for_match():
    b = Board()
    b.add("16:00:01.000", ">", "esp> ", key="p")
    b.add("16:00:01.200", "↪", "version", key="e")
    b.add("16:00:01.300", ">", "version: v2.4.1", key="v")
    assert rr(since="session", until="version")["end"] == b.cursor("v")       # sin echo: el eco
    r = rr(since="session", until="version", echo="version")
    assert r["until_found"] and r["match"] == "16:00:01.300 > version: v2.4.1"


def test_logical_line_split_between_polls():
    """El poll 1 ve `> result=`; el `↪ OK` llega después: el poll 2 desde end lo encuentra."""
    b = Board()
    b.add("16:00:01.000", ">", "I (5) app: result=", key="h")
    r1 = rr(since="session", until="result=OK")
    assert r1["until_found"] is False
    b.add("16:00:01.500", "|", "INFO  | protocol       | otro hilo")
    b.add("16:00:02.000", "↪", "OK", key="c")
    r2 = rr(since=r1["end"], until="result=OK")
    assert r2["until_found"] is True and r2["match"] == "16:00:01.000 > I (5) app: result=OK"
    assert r2["end"] == f"c:{SID}:{b.size}"
    assert [l.split(" ", 2)[1] for l in r2["lines"]] == ["|", "↪"]      # la salida: solo lo del rango


def test_response_as_continuation_of_prompt_before_send():
    b = Board()
    b.add("16:00:01.000", ">", "esp> ", key="prompt")
    before_send = f"c:{SID}:{b.size}"
    b.add("16:00:05.000", "↪", "OK done", key="c")
    r = rr(since=before_send, until="OK done")
    assert r["until_found"] and r["match"] == "16:00:01.000 > esp> OK done"


def test_seeded_line_that_already_matched_does_not_match_again():
    b = Board()
    b.add("16:00:01.000", ">", "ready ", key="h")
    end = f"c:{SID}:{b.size}"
    b.add("16:00:02.000", "↪", "y algo más")
    assert rr(since=end, until="ready")["until_found"] is False


def test_echo_seen_lets_the_client_stop_sending_echo():
    """Eco en el poll 1, respuesta que termina igual en el poll 2: con echo_seen el
    cliente deja de mandar echo y la respuesta matchea."""
    b = Board()
    b.add("16:00:00.500", ">", "esp> ", key="p")                        # prompt antes del send
    r1 = rr(since=f"c:{SID}:{b.size}", until="version", echo="version")
    assert r1["until_found"] is False and r1["echo_seen"] is None       # el eco todavía no llegó
    b.add("16:00:01.000", "↪", "version", key="e")
    r2 = rr(since=r1["end"], until="version", echo="version")
    assert r2["until_found"] is False and r2["echo_seen"] == b.cursor("p")   # `esp> ` + `↪ version`
    b.add("16:00:01.100", ">", "fw version", key="resp")
    r3 = rr(since=r2["end"], until="version")                           # ya sin echo
    assert r3["until_found"] and r3["match"] == "16:00:01.100 > fw version"


def test_echo_and_response_in_the_same_poll():
    b = Board()
    start = f"c:{SID}:{b.size}"
    b.add("16:00:01.000", ">", "esp> version", key="e")
    b.add("16:00:01.100", ">", "app version", key="resp")
    r = rr(since=start, until="version", echo="version")
    assert r["echo_seen"] == b.cursor("e") and r["match"] == "16:00:01.100 > app version"


def test_until_does_not_cross_sessions(b):
    r = rr(since="boot", until="re:nunca", live=False)
    assert r["until_found"] is False and r["session_ended"] is True


def test_bad_until_regex(b):
    with pytest.raises(RangeError) as e:
        rr(until="re:(")
    assert e.value.error == "bad_request"


# ---------- around ----------

def test_around_from_previous_boot_to_next_boot(b):
    b.add("16:00:04.000", ">", "rst:0x1 (POWERON_RESET)", key="boot2")
    b.event("boot", "boot2")
    r = rr(around="panic")
    assert r["start"] == b.cursor("boot0") and r["end"] == b.cursor("boot1")   # el siguiente, exclusive
    assert bodies(r)[-1].startswith("Backtrace")


def test_around_without_next_boot_goes_to_end_and_without_previous_from_session():
    b = Board()
    b.add("16:00:01.000", ">", "algo", key="x")
    b.add("16:00:02.000", ">", "Guru Meditation Error: Core  0 panic'ed (StoreProhibited)", key="p")
    b.add("16:00:02.100", ">", "Backtrace: 0x1", key="bt")
    b.event("panic", "p")
    r = rr(around="panic")
    assert r["start"] == f"c:{SID}:0" and r["end"] == f"c:{SID}:{b.size}"


def test_around_boot_starts_at_that_boot(b):
    assert rr(around="boot~1")["start"] == b.cursor("boot0")
    assert rr(around="boot~1")["end"] == b.cursor("boot1")


def test_around_before_after(b):
    r = rr(around="panic", before=1, after=1)
    assert r["start"] == b.cursor("tl0") and r["end"] == b.cursor("boot1")
    assert len(r["lines"]) == 3
    assert len(rr(around="panic", before=0)["lines"]) == 1


def test_around_excludes_since_until(b):
    with pytest.raises(RangeError):
        rr(around="panic", since="boot")
    with pytest.raises(RangeError):
        rr(since="boot", before=2)


# ---------- salida ----------

def test_max_lines_head_and_tail_with_marker():
    b = Board()
    for i in range(300):
        b.add("16:00:01.000", ">", f"l{i}")
    r = rr(max_lines=100)
    assert r["truncated"] and len(r["lines"]) == 101
    assert bodies(r)[:2] == ["INFO  | devicelog      | sesión 20261005_160000_42 tty=ttyUSB0", "l0"]
    assert r["lines"][50] == "… 201 líneas omitidas …"
    assert bodies(r)[-1] == "l299"
    assert len(rr()["lines"]) == 201                        # default 200 + marcador
    assert len(rr(max_lines=99999)["lines"]) == 301         # tope 5000: entra todo


def test_src_grep_and_raw(b):
    assert all(" | " not in l[:16] for l in rr(src="serial")["lines"])
    assert bodies(rr(src="taglog"))[1] == "INFO  | protocol       | algo del server"
    assert bodies(rr(grep="rst:"))[0].startswith("rst:0x1")
    assert len(rr(grep="rst:")["lines"]) == 2
    assert "\x1b[0;32m" in rr(raw=True)["lines"][2] and "\x1b" not in rr()["lines"][2]
    with pytest.raises(RangeError) as e:
        rr(src="otro")
    assert e.value.error == "bad_request"


def test_esptool_progress_is_collapsed():
    b = Board()
    b.add("16:00:01.000", "|", "INFO  | flash          | write_flash...")
    for pct in (10, 50, 100):
        b.add("16:00:02.000", "|", f"INFO  | flash          | Writing at 0x0001{pct:04d}... ({pct} %)")
    b.add("16:00:03.000", "|", "INFO  | flash          | Hash of data verified.")
    assert bodies(rr())[1:] == ["INFO  | flash          | write_flash...",
                                "INFO  | flash          | Writing at 0x00010100... (100 %)",
                                "INFO  | flash          | Hash of data verified."]


def test_lines_of_another_day_carry_the_date():
    b = Board()
    b.add("23:59:59.900", ">", "antes", day="2026-10-05")
    b.add("00:00:00.100", ">", "después", day="2026-10-06")
    assert rr()["lines"][-2:] == ["23:59:59.900 > antes", "2026-10-06 00:00:00.100 > después"]


def test_events_in_range(b):
    r = rr(since="boot~1", until="panic")
    assert [(e["type"], e["cursor"]) for e in r["events"]] == [("boot", b.cursor("boot0")),
                                                              ("panic", b.cursor("panic0"))]
    assert r["events"][1]["detail"] == {"kind": "guru"}


# ---------- list_events ----------

def le(**kw):
    kw.setdefault("now", epoch("16:10:00.000"))
    return logrange.list_events(paths.device_home(MAC), **kw)


def test_list_events_filters(b):
    b.event("send", "app0", ts="2026-10-05T16:00:01.100")
    r = le()
    assert [e["type"] for e in r["events"]] == ["boot", "send", "panic", "boot"]   # por offset
    assert r["session"] == SID
    assert [e["type"] for e in le(types="boot,panic")["events"]] == ["boot", "panic", "boot"]
    assert [e["cursor"] for e in le(since="panic")["events"]] == [b.cursor("panic0"), b.cursor("boot1")]
    assert len(le(limit=1)["events"]) == 1 and le(limit=1)["events"][0]["cursor"] == b.cursor("boot1")
    assert [e["type"] for e in le(since="16:00:01")["events"]] == ["send"]          # hora del evento
    with pytest.raises(RangeError):
        le(types="nada")


def test_list_events_crosses_sessions(b):
    ev = {"ts": "2026-10-05T15:00:00.000", "type": "flash", "cursor": f"c:{OLD}:10", "detail": {}}
    with open(b.home / "events.jsonl", "a") as f:
        f.write(json.dumps(ev) + "\n")
    assert le()["events"][0]["type"] == "flash"
    assert le(since="session")["events"][0]["type"] == "boot"


# ---------- events.jsonl sin tope: una lectura por pedido, de atrás para adelante ----------

def _old_session_events(b, n):
    """n eventos de una sesión anterior al principio de events.jsonl (meses de uso)."""
    path = b.home / "events.jsonl"
    current = path.read_bytes()
    with open(path, "wb") as f:
        for i in range(n):
            f.write(events.encode(events.make("panic", f"c:{OLD}:{i}", {"kind": "guru", "line": "x" * 100})))
        f.write(current)


def test_events_are_read_once_per_request(b, monkeypatch):
    """Antes read_range leía events.jsonl 2 o 3 veces por pedido (resolve,
    _find_event y los eventos del rango)."""
    reads = []
    for name in ("read", "read_back"):
        real = getattr(events, name)
        monkeypatch.setattr(events, name, lambda *a, _r=real, _n=name, **k: (reads.append(_n), _r(*a, **k))[1])
    b.event("send", "app1")
    for kw in ({"since": "panic", "until": "send"}, {"since": "boot~1", "until": "boot"}, {"around": "panic"},
               {"since": b.cursor("app0"), "until": "re:temperatura"}, {}):
        reads.clear()
        rr(**kw)
        assert len(reads) == 1, (kw, reads)


def test_poll_near_the_end_does_not_parse_the_whole_events_file(b, monkeypatch):
    """El poll de una espera (since = el end anterior) en la sesión actual lee la
    cola de events.jsonl: no parsea los eventos de las sesiones anteriores."""
    _old_session_events(b, 5000)
    b.event("send", "app1")
    parsed = []
    real = json.loads
    monkeypatch.setattr(json, "loads", lambda raw, *a, **k: (parsed.append(1), real(raw, *a, **k))[1])
    r = rr(since=b.cursor("app0"), until="send")
    assert r["until_found"] and [e["type"] for e in r["events"]] == ["panic", "boot", "send"]
    assert len(parsed) < logrange.EVENT_SLACK + 20      # la cola + el margen, no las 5000 anteriores
    parsed.clear()
    assert [e["type"] for e in rr(since="session")["events"]] == ["boot", "panic", "boot", "send"]
    assert len(parsed) < logrange.EVENT_SLACK + 20      # la cola + el margen, no las 5000 anteriores
    parsed.clear()
    r = le(limit=3)                                     # los últimos N: la cola
    assert [e["type"] for e in r["events"]] == ["panic", "boot", "send"] and r["more"] is True
    assert len(parsed) < logrange.EVENT_SLACK + 20      # la cola + el margen, no las 5000 anteriores
    parsed.clear()
    assert [e["type"] for e in le(since="session", types="send")["events"]] == ["send"]   # marcas del dashboard
    assert len(parsed) < logrange.EVENT_SLACK + 20      # la cola + el margen, no las 5000 anteriores


def test_last_events_tolerates_disorder_and_old_sessions(b):
    """Los últimos N leyendo hacia atrás dan lo mismo que ordenar todo (el api
    puede escribir un evento con cursor anterior después de otros)."""
    _old_session_events(b, 300)
    b.event("send", "app1")
    b.event("command", "app0")                          # escrito último, cursor anterior
    path = paths.device_home(MAC) / "events.jsonl"
    full = logrange.sort_events(events.read(path))
    for limit in (1, 2, 5, 50, 400):
        for types in (None, "panic", "send,command"):
            wanted = types.split(",") if types else []
            ref = [e for e in full if not wanted or e["type"] in wanted]
            r = le(limit=limit, types=types)
            assert [e["cursor"] for e in r["events"]] == [e["cursor"] for e in ref[-limit:]], (limit, types)
            assert r["more"] == (len(ref) > limit), (limit, types)


def test_counts_are_incremental_and_survive_a_replaced_file(b):
    path = paths.device_home(MAC) / "events.jsonl"
    assert le(counts=True)["counts"] == {"boot": 2, "panic": 1}
    b.event("send", "app1")
    with open(path, "ab") as f:
        f.write(b'{"ts":"2026-10-05T16:00:09.000","type":"panic"')     # a medio escribir: todavía no cuenta
    assert le(counts=True)["counts"] == {"boot": 2, "panic": 1, "send": 1}
    with open(path, "ab") as f:
        f.write(b',"cursor":"c:' + SID.encode() + b':5","detail":{},"by":"device"}\n')
    assert le(counts=True)["counts"] == {"boot": 2, "panic": 2, "send": 1}
    tmp = path.with_name("e.tmp")                       # migración: otro archivo (otro inode), más chico
    tmp.write_bytes(path.read_bytes().splitlines(keepends=True)[0])
    tmp.replace(path)
    assert le(counts=True)["counts"] == {"boot": 1}


# ---------- con el DeviceLog real ----------

def test_with_real_device_log():
    from server import taglog
    from server.device import DeviceManager
    taglog.clear_sinks()
    try:
        manager = DeviceManager("/dev/ttyUSB3", mac_reader=lambda: MAC, tcp_port=5003, publish_state=False)
        log = manager.device.device_log
        taglog.add_sink(log.taglog_sink)
        manager.discover()
        manager.on_serial(b"rst:0x1 (POWERON_RESET),boot:0x13\r\nI (1) app: hola\r\n")
        manager.on_serial(b"Guru Meditation Error: Core  1 panic'ed (LoadProhibited). \r\nBacktrace: 0x1\r\n")
        manager.on_serial(b"rst:0xc (SW_CPU_RESET),boot:0x13\r\n")
        log.close()
    finally:
        taglog.reset_default_sinks()
    r = logrange.read_range(paths.device_home(MAC), since="boot~1", until="panic")
    assert r["until_found"] and r["match"].endswith("> Guru Meditation Error: Core  1 panic'ed (LoadProhibited). ")
    assert [l.split(" ", 2)[2] for l in r["lines"]] == ["rst:0x1 (POWERON_RESET),boot:0x13", "I (1) app: hola",
                                                         "Guru Meditation Error: Core  1 panic'ed (LoadProhibited). "]
    assert rr(around="panic")["end"] == rr(since="boot")["start"]


# ---------- ReDoS: grep y until=re: llegan sin auth ----------

def redos_board():
    b = Board()
    for _ in range(20):
        b.add("16:00:01.000", ">", "a" * 26 + "!")
    return b


def test_pattern_too_long_is_rejected(b):
    for kw in (dict(grep="a" * 300), dict(until="re:" + "a" * 300), dict(until="a" * 300)):
        with pytest.raises(RangeError) as e:
            rr(**kw)
        assert e.value.error == "bad_request" and "256" in e.value.message


def test_without_regex_module_nested_quantifiers_are_rejected(monkeypatch):
    import time
    monkeypatch.setattr(logrange, "_regex", None)
    redos_board()
    t0 = time.monotonic()
    for kw in (dict(grep="(a+)+$"), dict(until="re:(a*)*!x"), dict(grep="(a|b+){2,}$"), dict(grep="(a|a)+$")):
        with pytest.raises(RangeError) as e:
            rr(**kw)
        assert e.value.error == "bad_request" and "anidados" in e.value.message
    assert time.monotonic() - t0 < 1
    assert len(rr(grep="(foo|a)!")["lines"]) == 20             # una alternancia sin cuantificar pasa
    assert len(rr(grep="a+!")["lines"]) == 20                   # y un cuantificador simple también


def test_regex_time_budget(monkeypatch):
    redos_board()
    monkeypatch.setattr(logrange, "REGEX_BUDGET", -1)
    with pytest.raises(RangeError) as e:
        rr(grep="a!")
    assert e.value.error == "bad_request" and "tarda demasiado" in e.value.message


def test_only_the_first_chars_of_a_line_are_evaluated():
    b = Board()
    b.add("16:00:01.000", ">", "x" * 5000 + "FIN")
    assert rr(grep="FIN")["lines"] == [] and rr(until="FIN")["until_found"] is False


@pytest.mark.skipif(logrange._regex is None, reason="sin el módulo regex (en la Pi viene de requirements.txt)")
def test_catastrophic_regex_times_out_with_regex_module():
    """`regex` optimiza (a+)+$ (no explota), pero una alternancia ambigua sí:
    tiene que cortar por timeout, no colgar el api."""
    import time
    redos_board()
    t0 = time.monotonic()
    rr(grep="(a+)+$")
    with pytest.raises(RangeError) as e:
        rr(grep="(a|a)+$")
    assert e.value.error == "bad_request" and time.monotonic() - t0 < 3


def test_iso_fractions_of_any_length():
    """fromisoformat de 3.9 rechaza fracciones que no sean de 3 o 6 dígitos."""
    now = epoch("16:10:00.000")
    assert logrange.parse_time("2026-10-05T16:02:03.1", now) == epoch("16:02:03.100")
    assert logrange.parse_time("2026-10-05T16:02:03.12345", now) == pytest.approx(epoch("16:02:03.000") + 0.12345)
    assert logrange.parse_time("2026-10-05 16:02:03.5-03:00", now) is not None


def test_list_events_more_and_order(b):
    r = le(limit=2)
    assert [e["cursor"] for e in r["events"]] == [b.cursor("panic0"), b.cursor("boot1")] and r["more"] is True
    r = le(limit=2, order="asc")
    assert [e["cursor"] for e in r["events"]] == [b.cursor("boot0"), b.cursor("panic0")] and r["more"] is True
    r = le(limit=2, order="asc", since=b.cursor("panic0"))
    assert [e["cursor"] for e in r["events"]] == [b.cursor("panic0"), b.cursor("boot1")] and r["more"] is False
    assert le()["more"] is False
    with pytest.raises(RangeError):
        le(order="x")


def test_list_events_counts_all_types_beyond_the_page(b):
    """El dashboard: un boot_loop enterrado bajo 150 panics tiene que verse en los chips."""
    r = le(limit=1, counts=True)
    assert r["counts"] == {"boot": 2, "panic": 1} and len(r["events"]) == 1
    assert le(limit=1, types="panic", counts=True)["counts"] == {"boot": 2, "panic": 1}   # sin el filtro de tipo
    assert le(since=b.cursor("panic0"), counts=True)["counts"] == {"panic": 1, "boot": 1}
    assert "counts" not in le()                                                          # el CLI no lo pide

"""
Tests para events.py: events.jsonl (append atómico entre procesos, lectura,
migración de una sesión) y cursores del log.
"""
import json
import multiprocessing
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server import events, paths
from server.device_log import DeviceLog

MAC = "AA:BB:CC:DD:EE:FF"
S1 = "20261005_160000_100"
S2 = "20261005_170000_200"


def test_cursor_roundtrip():
    c = events.format_cursor(S1, 4821)
    assert c == f"c:{S1}:4821"
    assert events.parse_cursor(c) == (S1, 4821)
    for bad in (None, "", "c:x:1", f"c:{S1}:-1", f"{S1}:1"):
        assert events.parse_cursor(bad) is None


def test_append_and_read_one_compact_line(tmp_path):
    f = tmp_path / "d" / "events.jsonl"
    ev = events.make("panic", f"c:{S1}:10", {"kind": "guru", "line": "ñ"}, ts=1_800_000_000.5)
    events.append(f, ev)
    events.append(f, events.make("boot", f"c:{S1}:20"))
    raw = f.read_bytes()
    assert raw.count(b"\n") == 2 and b": " not in raw.split(b"\n")[0]
    got = events.read(f)
    assert [e["type"] for e in got] == ["panic", "boot"]
    assert got[0]["detail"]["line"] == "ñ" and got[0]["by"] == "device"
    assert got[0]["ts"].endswith(".500") and "T" in got[0]["ts"]


def test_read_skips_invalid_lines(tmp_path):
    f = tmp_path / "events.jsonl"
    f.write_text('{"type":"boot"}\nbasura\n[1]\n{"type":"panic"}\n')
    assert [e["type"] for e in events.read(f)] == ["boot", "panic"]
    assert events.read(tmp_path / "no_existe.jsonl") == []


def test_long_event_is_truncated_below_one_write(tmp_path):
    ev = events.make("send", f"c:{S1}:0", {"text": "x" * 10000, "user": "alejo"}, by="api")
    data = events.encode(ev)
    assert len(data) <= events.MAX_EVENT_BYTES
    assert json.loads(data)["detail"]["user"] == "alejo"


def _writer(path, who, n):
    for i in range(n):
        events.append(pathlib.Path(path), events.make("send", f"c:{S1}:{i}",
                                                      {"text": who * 300, "i": i}, by=who))


def test_two_processes_appending_never_mix_lines(tmp_path):
    """device y api escriben el mismo events.jsonl: todas las líneas quedan válidas."""
    f = tmp_path / "events.jsonl"
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(f), who, 300)) for who in ("a", "b")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    lines = f.read_bytes().splitlines()
    assert len(lines) == 600
    parsed = [json.loads(line) for line in lines]
    for who in ("a", "b"):
        assert sorted(e["detail"]["i"] for e in parsed if e["by"] == who) == list(range(300))


def test_migrate_session_moves_only_current_session(tmp_path):
    src, dst = tmp_path / "unknown" / "events.jsonl", tmp_path / "mac" / "events.jsonl"
    events.append(src, events.make("boot", f"c:{S1}:1"))       # otra placa, sesión vieja
    events.append(src, events.make("session", f"c:{S2}:0"))
    events.append(src, events.make("boot", f"c:{S2}:50"))
    assert events.migrate_session(src, dst, S2) == 2
    assert [e["cursor"] for e in events.read(dst)] == [f"c:{S2}:0", f"c:{S2}:50"]
    assert [e["cursor"] for e in events.read(src)] == [f"c:{S1}:1"]
    assert events.migrate_session(src, dst, S1) == 1
    assert not src.exists()


def test_log_end_cursor_ignores_partial_last_line(tmp_path):
    log = tmp_path / "output.log"
    header = f"2026-10-05 16:00:00.000 | INFO  | devicelog      | sesión {S1} tty=ttyUSB0\n".encode()
    log.write_bytes(header + "2026-10-05 16:00:01.000 > año\n".encode() + b"2026-10-05 16:00:02.000 > a medi")
    end = len(header) + len("2026-10-05 16:00:01.000 > año\n".encode())
    assert events.log_end_cursor(log) == f"c:{S1}:{end}"
    assert events.read_session_id(log) == S1


def test_log_end_cursor_without_session_header(tmp_path):
    log = tmp_path / "output.log"
    log.write_text("log viejo\n")
    assert events.log_end_cursor(log) is None
    assert events.log_end_cursor(tmp_path / "no_existe.log") is None


def test_record_from_api_uses_end_of_current_log(monkeypatch, tmp_path):
    """La función para el proceso del api: el evento va al events.jsonl de la
    placa con el cursor del fin de la última línea del log escrito por el device."""
    monkeypatch.setenv("ESP_BASE", str(tmp_path))
    dlog = DeviceLog("ttyUSB0", autoflush=False)
    dlog.adopt(MAC)
    dlog.write_serial(b"esp> \n")
    ev = events.record(paths.device_output_log(MAC), "send", {"text": "help", "enter": True, "user": "x"})
    assert ev["cursor"] == dlog.end_cursor() and ev["by"] == "api"
    assert events.read(paths.device_events_file(MAC))[-1]["type"] == "send"
    dlog.close()


def test_migrate_keeps_remaining_file_writable_by_api(tmp_path):
    """Lo que queda en el provisorio se reescribe: tiene que seguir siendo 666
    (lo crea root, el api escribe como sfypi)."""
    src, dst = tmp_path / "unknown" / "events.jsonl", tmp_path / "mac" / "events.jsonl"
    events.append(src, events.make("boot", f"c:{S1}:1"))
    events.append(src, events.make("boot", f"c:{S2}:1"))
    events.migrate_session(src, dst, S2)
    assert src.stat().st_mode & 0o777 == 0o666


def test_log_end_cursor_uses_one_fd_across_rotation(monkeypatch, tmp_path):
    """El log rota (rename + archivo nuevo) entre la lectura del header y la de la
    cola: sesión y offset tienen que ser del mismo archivo."""
    log = tmp_path / "output.log"
    head1 = f"2026-10-05 16:00:00.000 | INFO  | devicelog      | sesión {S1} tty=ttyUSB0\n".encode()
    log.write_bytes(head1 + b"2026-10-05 16:00:01.000 > corta\n")
    real_open = open
    calls = []

    def rotating_open(path, *a, **kw):
        calls.append(path)
        if len(calls) == 2:          # segunda apertura: ya rotó
            log.rename(tmp_path / f"output_{S1}.log")
            log.write_bytes(f"2026-10-05 17:00:00.000 | INFO  | devicelog      | sesión {S2} tty=ttyUSB0\n"
                            .encode() + b"x" * 5000 + b"\n")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(events, "open", rotating_open, raising=False)
    cursor = events.log_end_cursor(log)
    session, offset = events.parse_cursor(cursor)
    assert (session, offset) == (S1, len(head1) + len(b"2026-10-05 16:00:01.000 > corta\n"))

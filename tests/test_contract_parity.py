"""Paridad de los contratos que están escritos dos veces (Python y JS, o server
y cliente): el prefijo de línea del DeviceLog, el cursor c:<sesión>:<offset> y
el nombre de sesión. Las regex están copiadas a propósito (el dashboard y el
cliente no importan el server); esto avisa si una cambia y la otra no. Lo que
corre node se saltea sin node. Igual que tests/test_linemark_parity.py."""
import json
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "remote"))
from client import espbench_lib  # noqa: E402
from server import device_log, events, history, logrange  # noqa: E402

EB = ROOT / "remote/dashboard/espbench.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node no instalado")


def node(fn: str, cases: list) -> list:
    """EB.<fn>(caso) para cada caso, en node."""
    script = ("const EB = require(process.argv[1]);"
              "const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              f"process.stdout.write(JSON.stringify(cases.map(c => EB.{fn}(c))));")
    r = subprocess.run(["node", "-e", script, str(EB)], input=json.dumps(cases), capture_output=True,
                       text=True, check=True)
    return json.loads(r.stdout)


PREFIX_CASES = [
    "2026-10-05 16:02:03.123 > I (120) app_init: App version: v2.4.1",
    "2026-10-05 16:02:05.002 | INFO  | device         | monitoring -> flashing",
    "2026-10-05 16:02:05.100 ↪ ...continuación",
    "2026-10-05 16:02:05.100 ↪ ",
    "2026-10-05 16:02:05.100 >",                    # sin el espacio del final: no
    "2026-10-05 16:02:05.10 > x",                   # 2 dígitos de ms: no
    "2026-10-05T16:02:05.100 > x",                  # con T: no
    "2026-10-05 16:02:05 | WARN  | protocol       | log viejo (taglog.format_line)",
    " 2026-10-05 16:02:05.100 > x",                 # no anclado al principio: no
    "2026-10-05 16:02:05.100 # x",                  # otro origen: no
    "I (120) main: log viejo sin prefijo",
    "",
]


@needs_node
def test_line_prefix_python_and_js_agree():
    js = node("splitPrefix", PREFIX_CASES)
    for case, j in zip(PREFIX_CASES, js):
        m = logrange._PREFIX_RE.match(case)
        py = (m.group(1), m.group(2), m.group(3), case[m.end():]) if m else (None, None, None, case)
        assert (j["date"], j["time"], j["origin"], j["body"]) == py, case
    # y Line (lo que usa /log) parte igual
    for case in PREFIX_CASES:
        line = logrange.Line(0, case.encode())
        m = logrange._PREFIX_RE.match(case)
        assert (line.origin, line.body) == ((m.group(3), case[m.end():]) if m else (None, case))


CURSOR_CASES = [
    "c:20261005_160203_812:48213",
    "c:20261005_160203_812:0",
    "c:20261005_160203_812001:5",                   # benchsim agrega dígitos al pid
    "c:20261005_160203_812:",
    "c:20261005_160203:12",
    "c:2026100_160203_812:12",
    "c:20261005_160203_812:12x",
    "x c:20261005_160203_812:12",
    "c:20261005_160203_812:-1",
    "C:20261005_160203_812:1",
    "session",
    "",
]


@needs_node
def test_cursor_python_js_and_client_agree():
    py = [list(c) if c else None for c in map(events.parse_cursor, CURSOR_CASES)]
    assert node("parseCursor", CURSOR_CASES) == py
    assert node("cursorSession", CURSOR_CASES) == [c[0] if c else None for c in py]
    assert node("cursorOffset", CURSOR_CASES) == [c[1] if c else None for c in py]
    assert [list(c) if c else None for c in map(espbench_lib.parse_cursor, CURSOR_CASES)] == py


def test_cursor_client_and_server_agree_without_node():
    for case in CURSOR_CASES:
        assert espbench_lib.parse_cursor(case) == events.parse_cursor(case), case


@needs_node
def test_session_name_device_log_history_and_js_agree():
    """device_log.make_session_id arma el id; el archivo rotado es
    output_<id>.log; history.SESSION_RE lo valida (llega por URL) y el dashboard
    saca la hora de inicio del nombre (EB.sessionStart)."""
    epoch = time.mktime((2026, 10, 5, 16, 2, 3, 0, 0, -1))
    sids = [device_log.make_session_id(epoch, pid) for pid in (1, 812, 4194304)]
    names = [f"output_{sid}.log" for sid in sids]
    bad = ["output_20261005_160203.log",              # log viejo (hora de rotación, sin pid): sin inicio
           "output_20261005_160203_812.txt", "output_../../etc.log", "xoutput_20261005_160203_1.log"]
    for sid, name in zip(sids, names):
        assert events.parse_cursor(events.format_cursor(sid, 7)) == (sid, 7)
        assert history.SESSION_RE.fullmatch(name), name
        assert events._session_from_header(f"| INFO  | devicelog | sesión {sid} tty=x".encode()) == sid
    assert history.SESSION_RE.fullmatch("output.log")
    assert node("sessionStart", names) == [time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))] * 3
    assert node("sessionStart", bad) == [None] * len(bad)
    assert not any(history.SESSION_RE.fullmatch(n) for n in bad[1:])

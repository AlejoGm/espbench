"""La corrida de tests de una placa para el dashboard (remote/server/testrun.py): avisos del cliente, vista de la
última corrida, ventanas; y lo que cambian en el resto: endpoint, `acked` del registry y la actividad por hora."""
import asyncio
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from fastapi import HTTPException  # noqa: E402

from server import api, benchinfo, events, locks, paths, runstate, testrun  # noqa: E402
from server.device_registry import acked  # noqa: E402

MAC = "AA:BB:CC:DD:EE:FF"
T0 = time.mktime((2026, 10, 26, 12, 0, 0, 0, 0, -1))


def start(run_id="r1", total=3, now=T0, **kw):
    return testrun.apply(MAC, {"phase": "start", "run_id": run_id, "total": total, "project": "nvc3_test",
                               "user": "alejo", **kw}, now, "c:S:0")


def case(i, now, run_id="r1", result=None, **kw):
    c = {"index": i, "number": i + 10, "name": f"caso {i}", **kw}
    if result:
        c["result"] = result
    return testrun.apply(MAC, {"phase": "result" if result else "case", "run_id": run_id, "case": c}, now,
                         f"c:S:{i * 100 + (50 if result else 0)}")


# ---------- testrun ----------

def test_a_run_reports_progress_and_result():
    assert start()["total"] == 3
    case(1, T0 + 1)
    v = testrun.view(MAC, T0 + 2)
    assert (v["status"], v["done"], v["current"]["name"]) == ("running", 0, "caso 1")
    case(1, T0 + 3, result="pass", duration_s=2)
    case(2, T0 + 4)
    case(2, T0 + 9, result="timeout", message="sin resultado en 5 s")
    v = testrun.view(MAC, T0 + 10)
    assert (v["done"], v["passed"], v["failed"], v["current"]) == (2, 1, 1, None)
    assert "cases" not in v                                  # /api/devices no lleva la lista
    end = testrun.apply(MAC, {"phase": "end", "run_id": "r1"}, T0 + 12, "c:S:999")
    assert (end["status"], end["failed"], end["duration_s"]) == ("done", 1, 12)
    d = testrun.detail(MAC, T0 + 13)
    assert d["status"] == "done" and [c["result"] for c in d["cases"]] == ["pass", "timeout"]
    # El caso guarda el cursor de su inicio (aviso case) y el de su resultado: el log de ese test
    assert (d["cases"][0]["cursor"], d["cases"][0]["end_cursor"]) == ("c:S:100", "c:S:150")


def test_a_run_without_news_for_stale_s_is_interrupted():
    start(stale_s=60)
    case(1, T0 + 10)
    assert testrun.view(MAC, T0 + 69)["status"] == "running"
    v = testrun.view(MAC, T0 + 71)
    assert (v["status"], v["current"]) == ("interrupted", None)
    assert testrun.windows(MAC, T0 + 500) == [(T0, T0 + 10)]       # la ventana termina en el último aviso


def test_windows_keep_the_previous_runs():
    start("r1")
    testrun.apply(MAC, {"phase": "end", "run_id": "r1"}, T0 + 50, None)
    start("r2", now=T0 + 100)
    assert testrun.windows(MAC, T0 + 120) == [(T0, T0 + 50), (T0 + 100, T0 + 120)]
    assert testrun.in_windows(T0 + 30, testrun.windows(MAC, T0 + 120))
    assert not testrun.in_windows(T0 + 70, testrun.windows(MAC, T0 + 120))


def test_bad_notices_are_rejected():
    with pytest.raises(testrun.TestRunError) as e:
        case(1, T0)                                            # sin start
    assert e.value.error == "not_found"
    start()
    for body in ({"phase": "x", "run_id": "r1"}, {"phase": "case", "run_id": "r 1"},
                 {"phase": "case", "run_id": "r1", "case": {"name": "sin index"}},
                 {"phase": "result", "run_id": "r1", "case": {"index": 1, "result": "ok"}}):
        with pytest.raises(testrun.TestRunError) as e:
            testrun.apply(MAC, body, T0, None)
        assert e.value.error == "bad_request"
    testrun.apply(MAC, {"phase": "end", "run_id": "r1"}, T0 + 1, None)
    assert testrun.apply(MAC, {"phase": "end", "run_id": "r1"}, T0 + 2, None)["repeated"]
    with pytest.raises(testrun.TestRunError):
        case(1, T0 + 3)                                        # ya terminó


# ---------- lo que cubre una corrida ----------

def iso(t):
    return events.iso_ms(t)


def test_panics_and_resets_inside_a_run_are_not_problems():
    health = {"last_panic": {"ts": iso(T0 + 5)}, "last_reset": {"ts": iso(T0 + 5), "abnormal": True},
              "boot_loop": True}
    wins = [(T0, T0 + 10)]
    assert acked(health, None, wins, now=T0 + 20) == {"panic": True, "reset": True, "boot_loop": True}
    # Un boot loop sigue cubierto un rato después del final (se cierra 60 s después del último boot)
    assert acked(health, None, wins, now=T0 + 200)["boot_loop"] is False
    later = {"last_panic": {"ts": iso(T0 + 30)}, "last_reset": None}
    assert acked(later, None, wins, now=T0 + 40) == {"panic": False, "reset": False, "boot_loop": True}
    assert acked(later, None, (), now=T0 + 40) is None         # sin ACK ni corridas: como antes


def test_activity_counts_test_restarts_apart():
    write = lambda e: events.append(paths.device_events_file(MAC), e)  # noqa: E731
    now = T0 + 3600
    write(events.make("boot", "c:S:1", {}, ts=T0 - 60))                       # antes: un reinicio de verdad
    write(events.make("test", "c:S:2", {"phase": "start", "run_id": "r1"}, ts=T0))
    write(events.make("boot", "c:S:3", {}, ts=T0 + 5))
    write(events.make("panic", "c:S:4", {"kind": "guru"}, ts=T0 + 6))
    write(events.make("test", "c:S:5", {"phase": "result", "case": {"result": "panic"}}, ts=T0 + 8))
    write(events.make("test", "c:S:6", {"phase": "result", "case": {"result": "pass"}}, ts=T0 + 9))
    a = benchinfo.device_activity(paths.device_events_file(MAC), 24, now, test_windows=[(T0, T0 + 10)])
    tot = {k: sum(b[k] for b in a["buckets"]) for k in ("boot", "panic") + benchinfo.TEST_KEYS}
    assert tot == {"boot": 1, "panic": 0, "test_boot": 1, "test_panic": 1, "test": 2, "test_fail": 1}
    assert a["totals"]["panic"] == 0 and a["recent"] == []    # bench-master no avisa el panic de un test


# ---------- endpoint ----------

def run(coro):
    return asyncio.run(coro)


def board(tty="ttyUSB0"):
    log = paths.device_output_log(MAC)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"2026-10-26 12:00:00.000 | INFO  | devicelog      | sesion 20261026_120000_1 tty=x\n")
    runstate.write(tty, {"mac": MAC, "state": "monitoring", "log_path": str(log)})


def test_endpoint_records_events_and_returns_the_run():
    board()
    r = run(api.device_test("ttyUSB0", {"phase": "start", "run_id": "r1", "total": 2, "lock_user": "alejo"}))
    assert r["run"]["status"] == "running" and r["cursor"]
    run(api.device_test("ttyUSB0", {"phase": "case", "run_id": "r1", "case": {"index": 1, "number": 4, "name": "a"}}))
    (ev_start, ev_case) = [e for e in events.read(paths.device_events_file(MAC)) if e["type"] == "test"]
    assert ev_start["detail"]["phase"] == "start" and ev_case["detail"]["case"]["name"] == "a"
    assert ev_case["by"] == "api"
    assert api.board_test(MAC)["run"]["current"]["number"] == 4


def test_endpoint_errors():
    board()
    with pytest.raises(HTTPException) as e:
        run(api.device_test("ttyUSB0", {"phase": "case", "run_id": "nada", "case": {"index": 1}}))
    assert (e.value.status_code, e.value.detail["error"]) == (404, "not_found")
    with pytest.raises(HTTPException) as e:
        run(api.device_test("ttyUSB0", {"phase": "start", "run_id": "r1", "expect_mac": "11:22:33:44:55:66"}))
    assert e.value.detail["error"] == "device_changed"
    # Reservada por otro: como send, 423
    locks.write("ttyUSB0", locks.Lock("juan", "t", int(time.time()) + 600, MAC))
    with pytest.raises(HTTPException) as e:
        run(api.device_test("ttyUSB0", {"phase": "start", "run_id": "r1", "lock_user": "alejo", "lock_token": "x"}))
    assert e.value.status_code == 423


def test_devices_carry_the_last_run_and_its_windows_cover_the_health():
    board()
    run(api.device_test("ttyUSB0", {"phase": "start", "run_id": "r1", "total": 1}))
    st = runstate.read("ttyUSB0")
    st["health"] = {"panics": 1, "last_panic": {"ts": events.iso_ms(time.time())}, "boot_loop": False}
    runstate.write("ttyUSB0", st)
    from server.device_registry import DeviceRegistry
    dev_dir = paths.devices_dir().parent / "dev"
    dev_dir.mkdir(exist_ok=True)
    (dev_dir / "ttyUSB0").touch()
    d = DeviceRegistry(dev_dir=str(dev_dir)).get_device("ttyUSB0")
    assert d.test["status"] == "running" and d.test["total"] == 1
    assert d.acked["panic"] is True


def test_the_api_writes_the_run_outside_the_device_dir():
    """devices/<MAC>/ lo crea el proceso del device (root, 755): el api (sfypi) no puede crear archivos ahí."""
    home = paths.device_home(MAC)
    home.mkdir(parents=True)
    home.chmod(0o555)
    try:
        start()
        assert testrun.view(MAC, T0)["status"] == "running"
    finally:
        home.chmod(0o755)

"""Tests de los endpoints de historial y consola de api.py. Sin TestClient (no
hay httpx): se llaman los handlers directo."""
import asyncio
import os
import pathlib
import sys
import time
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from fastapi import HTTPException

from server import api, locks, paths, runstate

MAC = "AA:BB:CC:DD:EE:FF"


@pytest.fixture
def tmux(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(api.subprocess, "run", fake_run)
    return calls


def run(coro):
    return asyncio.run(coro)


def test_send_text_literal_then_enter(tmux):
    r = run(api.device_send("ttyUSB0", {"text": "help C-r"}))
    assert r["ok"]
    assert tmux == [["tmux", "send-keys", "-t", "esp32_ttyUSB0", "-l", "help C-r"],
                    ["tmux", "send-keys", "-t", "esp32_ttyUSB0", "Enter"]]


def test_send_without_enter(tmux):
    run(api.device_send("esp-slot3", {"text": "x", "enter": False}))
    assert tmux == [["tmux", "send-keys", "-t", "esp32_esp-slot3", "-l", "x"]]


@pytest.mark.parametrize("tty,body,code", [
    ("ttyUSB0;rm", {"text": "x"}, 400),
    ("ttyUSB0", {"text": "a\x03b"}, 400),
    ("ttyUSB0", {"text": "x" * 300}, 400),
    ("ttyUSB0", {"text": "", "enter": False}, 400),
])
def test_send_rejects_bad_input(tmux, tty, body, code):
    with pytest.raises(HTTPException) as e:
        run(api.device_send(tty, body))
    assert e.value.status_code == code and tmux == []


def test_send_rejected_while_flashing(tmux):
    runstate.write("ttyUSB0", {"state": "flashing", "pid": 1})
    with pytest.raises(HTTPException) as e:
        run(api.device_send("ttyUSB0", {"text": "x"}))
    assert e.value.status_code == 409 and tmux == []


def test_jobs_and_sessions_use_mac_from_runstate():
    runstate.write("ttyUSB0", {"mac": MAC, "log_path": str(paths.device_output_log(MAC))})
    job = paths.device_jobs_dir(MAC) / "job_20261005_100000_b1"
    job.mkdir(parents=True)
    (job / "job.log").write_text("log del job\n")
    paths.device_output_log(MAC).write_text("serial\n")
    assert run(api.device_jobs("ttyUSB0"))[0]["job_id"] == "job_20261005_100000_b1"
    assert "log del job" in run(api.device_job_log("ttyUSB0", "job_20261005_100000_b1"))
    assert run(api.device_sessions("ttyUSB0"))[0]["name"] == "output.log"
    assert run(api.device_session("ttyUSB0", "output.log")).body == b"serial\n"
    with pytest.raises(HTTPException):
        run(api.device_session("ttyUSB0", "../devices.json"))


def test_history_routes_are_not_shadowed_by_device_path_route():
    """/api/device/{tty:path} se come todo: las rutas de historial tienen que ir antes."""
    paths_in_order = [r.path for r in api.app.routes]
    catch_all = paths_in_order.index("/api/device/{tty:path}")
    for p in ("/api/device/{tty}/jobs", "/api/device/{tty}/sessions", "/api/device/{tty}/send",
              "/api/board/{key}/log", "/api/board/{key}/events"):
        assert paths_in_order.index(p) < catch_all


def test_send_and_command_without_tmux_are_502(monkeypatch):
    def no_tmux(cmd, **kw):
        raise FileNotFoundError("tmux")
    monkeypatch.setattr(api.subprocess, "run", no_tmux)
    with pytest.raises(HTTPException) as e:
        run(api.device_send("ttyUSB0", {"text": "x"}))
    assert e.value.status_code == 502
    with pytest.raises(HTTPException) as e:
        run(api.device_command("ttyUSB0", "reset"))
    assert e.value.detail["error"] == "unexpected" and e.value.status_code == 502


def test_static_files_are_revalidated():
    """Después de un update el navegador tiene que pedir el JS/CSS nuevo."""
    static = next(r for r in api.app.routes if getattr(r, "name", "") == "static").app
    scope = {"type": "http", "method": "GET", "path": "/style.css", "headers": []}
    response = run(static.get_response("style.css", scope))
    assert response.headers["cache-control"] == "no-cache"


def test_unlock_expired_reservation_is_like_no_lock():
    paths.lock_file("ttyUSB0").parent.mkdir(parents=True)
    paths.lock_file("ttyUSB0").write_text("alejo:t0k:1000")
    r = run(api.device_unlock("ttyUSB0", {"lock_user": "juan", "lock_token": "x"}))
    assert r["message"] == "no estaba bloqueado"


# ---------- escrituras: reservas, expect_mac, evento send (fase 2) ----------

SID = "20261005_160000_42"


def board(tty="ttyUSB0", mac=MAC, lines=("> rst:0x1 (POWERON_RESET)",)):
    """Placa con proceso publicado y un log con header de sesión."""
    log = paths.device_output_log(mac)
    log.parent.mkdir(parents=True, exist_ok=True)
    body = f"2026-10-05 16:00:00.000 | INFO  | devicelog      | sesión {SID} tty={tty}\n"
    body += "".join(f"2026-10-05 16:00:01.000 {l}\n" for l in lines)
    log.write_bytes(body.encode())
    runstate.write(tty, {"mac": mac, "state": "monitoring", "log_path": str(log)})
    return log


def api_events(mac=MAC):
    from server import events
    return events.read(paths.device_events_file(mac))


def err(e):
    return e.value.status_code, e.value.detail["error"]


def test_send_returns_cursor_before_send_and_records_event(tmux):
    log = board()
    size = log.stat().st_size
    r = run(api.device_send("ttyUSB0", {"text": "help", "lock_user": "alejo"}))
    assert r["cursor"] == f"c:{SID}:{size}"
    (ev,) = [e for e in api_events() if e["type"] == "send"]
    assert ev["cursor"] == r["cursor"] and ev["by"] == "api"
    assert ev["detail"] == {"text": "help", "enter": True, "user": "alejo"}


def test_send_failed_is_not_an_event(monkeypatch):
    board()
    monkeypatch.setattr(api.subprocess, "run",
                        lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="no session"))
    with pytest.raises(HTTPException) as e:
        run(api.device_send("ttyUSB0", {"text": "x"}))
    assert e.value.status_code == 502 and api_events() == []


def test_send_expect_mac(tmux):
    board()
    with pytest.raises(HTTPException) as e:
        run(api.device_send("ttyUSB0", {"text": "x", "expect_mac": "11:22:33:44:55:66"}))
    assert err(e) == (409, "device_changed") and tmux == []
    assert run(api.device_send("ttyUSB0", {"text": "x", "expect_mac": "aabbccddeeff"}))["ok"]


def test_send_busy_has_error_code(tmux):
    runstate.write("ttyUSB0", {"state": "flashing", "pid": 1})
    with pytest.raises(HTTPException) as e:
        run(api.device_send("ttyUSB0", {"text": "x"}))
    assert err(e) == (409, "busy")


def reserve(user="alejo", token="t0k", **extra):
    return run(api.device_reserve("ttyUSB0", {"lock_user": user, "lock_token": token, **extra}))


def test_reserve_writes_lock_with_expiry_and_mac():
    import time
    from server import locks
    board()
    r = reserve(ttl_s=600)
    lock = locks.read("ttyUSB0")
    assert lock.user == "alejo" and lock.mac == "AABBCCDDEEFF"
    assert abs(lock.expires - (time.time() + 600)) < 5 and r["expires"] == lock.expires_iso_tz()
    import datetime as dt
    assert dt.datetime.fromisoformat(r["expires"]).timestamp() == lock.expires        # con zona, como lock_expires
    (ev,) = api_events()
    assert ev["type"] == "reserve" and ev["detail"]["user"] == "alejo" and ev["cursor"].startswith(f"c:{SID}:")
    assert ev["detail"]["expires"] == r["expires"] and dt.datetime.fromisoformat(r["expires"]).utcoffset() is not None
    run(api.device_release("ttyUSB0", {"lock_user": "alejo", "lock_token": "t0k"}))
    assert api_events()[-1]["detail"]["expires"] == r["expires"]


def test_reserve_conflicts():
    board()
    reserve()
    with pytest.raises(HTTPException) as e:
        reserve(user="juan")
    assert err(e) == (409, "locked") and "alejo" in e.value.detail["message"]
    with pytest.raises(HTTPException) as e:
        reserve(token="otro")
    assert err(e) == (403, "token_mismatch")
    with pytest.raises(HTTPException) as e:
        reserve(user="juan", expect_mac="11:22:33:44:55:66")
    assert err(e) == (409, "device_changed")


def test_reserve_and_release_with_colon_in_token():
    from server import locks
    board()
    reserve(token="a:123")
    assert locks.read("ttyUSB0") == locks.Lock("alejo", "a:123", locks.read("ttyUSB0").expires, "AABBCCDDEEFF")
    assert run(api.device_release("ttyUSB0", {"lock_user": "alejo", "lock_token": "a:123"}))["message"] == "liberado"


def test_reserve_max_is_24h():
    board()
    assert api.RESERVE_MAX_S == 24 * 3600
    assert reserve(ttl_s=24 * 3600)["ok"]


def test_reserve_over_own_flash_lock_and_renew():
    from server import locks
    board()
    locks.write("ttyUSB0", locks.Lock("alejo", "t0k"))      # lo dejó su flash
    first = reserve(ttl_s=60)["expires"]
    assert reserve(ttl_s=3600)["expires"] > first


def test_reserve_blocked_by_flash_lock_of_other_user():
    from server import locks
    board()
    locks.write("ttyUSB0", locks.Lock("juan", "x"))
    with pytest.raises(HTTPException) as e:
        reserve()
    assert err(e) == (409, "locked")


@pytest.mark.parametrize("body", [
    {"lock_user": "alejo"},
    {"lock_user": "alejo", "lock_token": "a\nb"},
    {"lock_user": "alejo", "lock_token": "t", "ttl_s": 0},
    {"lock_user": "alejo", "lock_token": "t", "ttl_s": "x"},
    {"lock_user": "alejo", "lock_token": "t", "ttl_s": 24 * 3600 + 1},      # tope 24 h (antes 7 días)
])
def test_reserve_bad_input(body):
    with pytest.raises(HTTPException) as e:
        run(api.device_reserve("ttyUSB0", body))
    assert err(e) == (400, "bad_request")


def test_release():
    from server import locks
    board()
    reserve()
    with pytest.raises(HTTPException) as e:
        run(api.device_release("ttyUSB0", {"lock_user": "juan", "lock_token": "x"}))
    assert err(e) == (403, "token_mismatch")
    assert run(api.device_release("ttyUSB0", {"lock_user": "alejo", "lock_token": "t0k"}))["ok"]
    assert locks.read("ttyUSB0") is None
    assert [e["type"] for e in api_events()] == ["reserve", "release"]


def test_reservation_blocks_send_and_command_of_others(tmux):
    board()
    reserve()
    for call in (lambda b: api.device_send("ttyUSB0", {"text": "x", **b}),
                 lambda b: api.device_command("ttyUSB0", "reset", b)):
        with pytest.raises(HTTPException) as e:
            run(call({}))
        assert err(e) == (423, "locked")
        with pytest.raises(HTTPException):
            run(call({"lock_user": "alejo", "lock_token": "otro"}))
        assert run(call({"lock_user": "alejo", "lock_token": "t0k"}))["ok"]   # el dueño
        assert run(call({"force": True}))["ok"]                                # el dashboard, forzando


def test_flash_lock_does_not_block_send(tmux):
    from server import locks
    board()
    locks.write("ttyUSB0", locks.Lock("juan", "x"))
    assert run(api.device_send("ttyUSB0", {"text": "x"}))["ok"]
    assert run(api.device_command("ttyUSB0", "reset"))["ok"]


def test_command_validates_tty_and_expect_mac(tmux):
    with pytest.raises(HTTPException) as e:
        run(api.device_command("ttyUSB0;reboot", "reset"))
    assert err(e) == (400, "bad_request") and tmux == []
    board()
    with pytest.raises(HTTPException) as e:
        run(api.device_command("ttyUSB0", "reset", {"expect_mac": "11:22:33:44:55:66"}))
    assert err(e) == (409, "device_changed") and tmux == []


def test_unlock_validates_tty():
    with pytest.raises(HTTPException) as e:
        run(api.device_unlock("../devices.json", {"lock_user": "a", "lock_token": "b"}))
    assert e.value.status_code == 400


# ---------- token (A2) ----------

def set_token(text):
    paths.api_token_file().parent.mkdir(parents=True, exist_ok=True)
    paths.api_token_file().write_text(text)


def test_without_token_file_writes_are_open(tmux):
    assert run(api.device_send("ttyUSB0", {"text": "x"}))["ok"]


def test_empty_token_file_is_no_token(tmux):
    set_token("\n")
    assert run(api.device_send("ttyUSB0", {"text": "x"}))["ok"]


@pytest.mark.parametrize("call", [
    lambda a: api.device_send("ttyUSB0", {"text": "x"}, authorization=a),
    lambda a: api.device_reserve("ttyUSB0", {"lock_user": "u", "lock_token": "t"}, authorization=a),
    lambda a: api.device_release("ttyUSB0", {"lock_user": "u", "lock_token": "t"}, authorization=a),
    lambda a: api.device_unlock("ttyUSB0", {"lock_user": "u", "lock_token": "t"}, authorization=a),
    lambda a: api.device_command("ttyUSB0", "reset", None, authorization=a),
    lambda a: api.devremote_reset("ttyUSB0", authorization=a),
    lambda a: api.patch_device(MAC, {"device_key": "X"}, authorization=a),
])
def test_token_required_on_writes(tmux, call, monkeypatch):
    from server.device_registry import DevicesFile
    monkeypatch.setattr(api, "_devices_file", DevicesFile(paths.devices_file()))   # el del módulo es de /opt/esp
    set_token("s3cret\n")
    for bad in (None, "s3cret", "Bearer otro", "Basic s3cret"):
        with pytest.raises(HTTPException) as e:
            run(call(bad))
        assert err(e) == (401, "auth")
    assert tmux == []
    try:
        run(call("Bearer s3cret"))      # pasa la auth (lo que haga después no importa acá)
    except HTTPException as e:
        assert e.status_code != 401


def test_token_does_not_close_reads():
    set_token("s3cret")
    assert run(api.device_jobs("ttyUSB0")) == []


# ---------- lecturas por placa: /api/board/{key}/log|events ----------

def registered(key="OEM_NOVUS"):
    from server.device_registry import DevicesFile
    DevicesFile().update_device_key(MAC, key)


def test_board_log_by_key_sn_or_mac_with_board_disconnected():
    """Sin proceso (placa desenchufada): se lee igual, y la sesión es la última."""
    from common import mac_to_sn_sfy
    board(lines=("> rst:0x1 (POWERON_RESET)", "> Guru Meditation Error: Core  1 panic'ed (X)"))
    runstate.remove("ttyUSB0")
    registered()
    for key in ("OEM_NOVUS", mac_to_sn_sfy(MAC), MAC, "aabbccddeeff", "AA-BB-CC-DD-EE-FF"):
        r = api.board_log(key, since="session", until="panic")
        assert r["until_found"] and r["start"] == f"c:{SID}:0", key
        assert r["session_ended"] is True                # nadie más escribe esa sesión


def test_board_log_live_session_not_ended():
    import os
    log = board()
    runstate.write("ttyUSB0", {"mac": MAC, "state": "monitoring", "log_path": str(log), "pid": os.getpid()})
    assert api.board_log(MAC, until="re:nunca")["session_ended"] is False
    runstate.write("ttyUSB0", {"mac": MAC, "state": "disconnected", "log_path": str(log), "pid": os.getpid()})
    assert api.board_log(MAC, until="re:nunca")["session_ended"] is True


def test_board_log_errors():
    board()
    registered()
    cases = [(dict(key="NO_EXISTE"), 404, "not_found"),
             (dict(key=MAC, since="ayer"), 400, "bad_anchor"),
             (dict(key=MAC, since="c:20200101_000000_1:0"), 410, "cursor_expired"),
             (dict(key=MAC, grep="("), 400, "bad_request")]
    for kw, status, error in cases:
        with pytest.raises(HTTPException) as e:
            api.board_log(**kw)
        assert err(e) == (status, error), kw


def test_board_events():
    from server import events
    log = board()
    events.append(paths.device_events_file(MAC), events.make("boot", f"c:{SID}:{len(log.read_bytes()) - 10}"))
    events.append(paths.device_events_file(MAC), events.make("send", f"c:{SID}:0", by="api"))
    r = api.board_events(MAC)
    assert [e["type"] for e in r["events"]] == ["send", "boot"] and r["session"] == SID
    assert [e["type"] for e in api.board_events(MAC, type="boot")["events"]] == ["boot"]
    assert api.board_events(MAC, type="boot", limit="1", counts=True)["counts"] == {"boot": 1, "send": 1}
    with pytest.raises(HTTPException) as e:
        api.board_events(MAC, type="xyz")
    assert err(e) == (400, "bad_request")


def test_unreadable_token_fails_closed(tmux):
    """Un api_token que existe pero no se lee (directorio, permisos, no es
    texto) no es "sin token": las escrituras se rechazan."""
    paths.api_token_file().mkdir(parents=True)
    with pytest.raises(HTTPException) as e:
        run(api.device_send("ttyUSB0", {"text": "x"}))
    assert err(e) == (500, "auth_config") and tmux == []
    paths.api_token_file().rmdir()
    paths.api_token_file().write_bytes(b"\xff\xfe")
    with pytest.raises(HTTPException) as e:
        run(api.device_send("ttyUSB0", {"text": "x"}, authorization="Bearer x"))
    assert err(e) == (500, "auth_config")


def test_non_ascii_bearer_is_401_not_500(tmux):
    set_token("s3cret")
    with pytest.raises(HTTPException) as e:
        run(api.device_send("ttyUSB0", {"text": "x"}, authorization="Bearer ñandú"))
    assert err(e) == (401, "auth")


def test_reserve_waits_for_the_lock_of_another_process(monkeypatch):
    """reserve lee-decide-escribe dentro de locks.exclusive: un flash que toma el
    lock en el medio no se pisa."""
    import threading, time as _t
    from server import locks, protocol
    board()
    inside, results = threading.Event(), []

    def flash_in_other_process():
        with locks.exclusive("ttyUSB0"):
            inside.set()
            _t.sleep(0.2)
            locks.write("ttyUSB0", locks.Lock("juan", "x"))      # LockStore.acquire de juan
    t = threading.Thread(target=flash_in_other_process)
    t.start()
    inside.wait(2)
    with pytest.raises(HTTPException) as e:
        reserve()
    t.join()
    assert err(e) == (409, "locked") and locks.read("ttyUSB0").user == "juan"


# ---------- revisión de la fase 2 ----------

def test_force_must_be_boolean_true(tmux):
    board()
    reserve()
    for bad in ("false", "true", 1, "yes"):
        with pytest.raises(HTTPException) as e:
            run(api.device_send("ttyUSB0", {"text": "x", "force": bad}))
        assert err(e) == (423, "locked"), bad
    assert tmux == []


def test_forced_writes_are_recorded_with_user(tmux):
    board()
    reserve()
    run(api.device_send("ttyUSB0", {"text": "x", "force": True, "lock_user": "dash"}))
    run(api.device_command("ttyUSB0", "reset", {"force": True}))
    send = [e for e in api_events() if e["type"] == "send"][0]
    cmd = [e for e in api_events() if e["type"] == "command"][0]
    assert send["detail"]["forced"] is True and send["detail"]["user"] == "dash"
    assert send["detail"]["by_user"] == "dash" and send["detail"]["by_host"] is None
    assert cmd["detail"] == {"command": "reset", "user": None, "forced": True, "by_user": None, "by_host": None}


def test_forced_event_records_the_host_of_the_request(tmux):
    board()
    reserve()
    req = types.SimpleNamespace(client=types.SimpleNamespace(host="10.0.0.7"))
    run(api.device_send("ttyUSB0", {"text": "x", "force": True}, request=req))
    send = [e for e in api_events() if e["type"] == "send"][0]["detail"]
    assert send["by_host"] == "10.0.0.7" and send["by_user"] is None


def test_command_records_event_with_cursor(tmux):
    log = board()
    r = run(api.device_command("ttyUSB0", "reset", {"lock_user": "alejo"}))
    assert r["cursor"] == f"c:{SID}:{log.stat().st_size}"
    (ev,) = api_events()
    assert ev["type"] == "command" and ev["detail"] == {"command": "reset", "user": "alejo"}


def test_command_cursor_is_taken_before_the_keys(monkeypatch):
    """El rst: de un reset sale en milisegundos: si el cursor se toma después de
    mandar las teclas, queda después del boot y reset --verify no lo ve."""
    log = board()
    before = log.stat().st_size

    def tmux_that_resets(cmd, **kw):
        with open(log, "ab") as f:
            f.write(b"2026-10-05 16:00:02.000 > rst:0xc (SW_CPU_RESET),boot:0x13\n")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(api.subprocess, "run", tmux_that_resets)
    r = run(api.device_command("ttyUSB0", "reset"))
    assert r["cursor"] == f"c:{SID}:{before}"
    assert api_events()[0]["cursor"] == r["cursor"]


def test_command_tmux_failure_is_502(monkeypatch):
    board()
    monkeypatch.setattr(api.subprocess, "run",
                        lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="no session"))
    with pytest.raises(HTTPException) as e:
        run(api.device_command("ttyUSB0", "reset"))
    assert err(e) == (502, "session_down") and "no session" in e.value.detail["message"]
    assert api_events() == []


def test_command_busy_while_flashing_or_erasing(tmux):
    for st in ("flashing", "erasing"):
        runstate.write("ttyUSB0", {"state": st, "pid": 1})
        with pytest.raises(HTTPException) as e:
            run(api.device_command("ttyUSB0", "reset"))
        assert err(e) == (409, "busy")
    assert tmux == []


def test_require_reservation(tmux):
    board()
    owner = {"lock_user": "alejo", "lock_token": "t0k", "require_reservation": True}
    with pytest.raises(HTTPException) as e:                       # sin reserva
        run(api.device_send("ttyUSB0", {"text": "x", **owner}))
    assert err(e) == (423, "reservation_lost")
    reserve()
    assert run(api.device_send("ttyUSB0", {"text": "x", **owner}))["ok"]
    assert run(api.device_command("ttyUSB0", "reset", owner))["ok"]
    run(api.device_release("ttyUSB0", {"lock_user": "alejo", "lock_token": "t0k"}))
    reserve(user="juan")
    for call in (lambda: api.device_send("ttyUSB0", {"text": "x", **owner}),
                 lambda: api.device_command("ttyUSB0", "reset", owner)):
        with pytest.raises(HTTPException) as e:
            run(call())
        assert err(e) == (423, "reservation_lost") and "juan" in e.value.detail["message"]


def test_reserve_requires_known_mac():
    runstate.write("ttyUSB0", {"state": "discovering", "mac": None})
    with pytest.raises(HTTPException) as e:
        reserve()
    assert err(e) == (409, "busy") and "MAC" in e.value.detail["message"]


def test_reserve_against_flash_lock_suggests_unlock():
    from server import locks
    board()
    locks.write("ttyUSB0", locks.Lock("juan", "x"))
    with pytest.raises(HTTPException) as e:
        reserve()
    assert err(e) == (409, "locked") and "unlock" in e.value.detail["message"]


def test_board_live_with_stale_state_of_another_tty():
    """La placa estuvo en ttyUSB0 (queda disconnected a propósito) y ahora vive en ttyUSB1."""
    import os
    log = board()
    runstate.write("ttyUSB0", {"mac": MAC, "state": "disconnected", "log_path": str(log), "pid": os.getpid()})
    runstate.write("ttyUSB1", {"mac": MAC, "state": "monitoring", "log_path": str(log), "pid": os.getpid()})
    assert api.board_log(MAC, until="re:nunca")["session_ended"] is False


def test_patch_device_validates_mac(monkeypatch):
    from server.device_registry import DevicesFile
    monkeypatch.setattr(api, "_devices_file", DevicesFile(paths.devices_file()))
    for bad in ("../x", "AABBCC", "GG:BB:CC:DD:EE:FF"):
        with pytest.raises(HTTPException) as e:
            run(api.patch_device(bad, {"device_key": "X"}))
        assert err(e) == (400, "bad_request")
    with pytest.raises(HTTPException) as e:
        run(api.patch_device(MAC, {}))
    assert err(e) == (400, "bad_request")
    assert run(api.patch_device("aabbccddeeff", {"device_key": "X"}))["ok"]
    assert DevicesFile(paths.devices_file()).get_all() == {MAC: {"device_key": "X", "hw_model": None}}


def test_unlock_errors_are_structured():
    from server import locks
    locks.write("ttyUSB0", locks.Lock("juan", "x"))
    with pytest.raises(HTTPException) as e:
        run(api.device_unlock("ttyUSB0", {"lock_user": "alejo", "lock_token": "t"}))
    assert err(e) == (403, "token_mismatch")
    with pytest.raises(HTTPException) as e:
        run(api.device_unlock("ttyUSB0", {}))
    assert err(e) == (400, "bad_request")


def test_unlock_records_release_and_force_drops_without_the_pair():
    """El dashboard: 'liberar' con el par y 'forzar' (force: true, con el token de
    la API) sin el par. Los dos quedan como evento release: el dueño anterior y
    quién forzó."""
    from server import locks
    board()
    reserve()
    set_token("s3cret")
    bearer = "Bearer s3cret"
    with pytest.raises(HTTPException) as e:
        run(api.device_unlock("ttyUSB0", {"force": "yes"}, authorization=bearer))   # solo el booleano fuerza
    assert err(e) == (400, "bad_request") and locks.read("ttyUSB0") is not None
    req = types.SimpleNamespace(client=types.SimpleNamespace(host="10.0.0.9"))
    r = run(api.device_unlock("ttyUSB0", {"force": True, "lock_user": "dash"}, authorization=bearer, request=req))
    assert r["ok"] and r["forced"] and r["user"] == "alejo" and locks.read("ttyUSB0") is None
    rel = [e for e in api_events() if e["type"] == "release"]
    d = rel[-1]["detail"]
    assert d["user"] == "alejo" and d["forced"] is True and d["expires"]
    assert d["by_user"] == "dash" and d["by_host"] == "10.0.0.9"
    assert run(api.device_unlock("ttyUSB0", {"force": True}, authorization=bearer))["message"] == "no estaba bloqueado"
    paths.api_token_file().unlink()
    locks.write("ttyUSB0", locks.Lock("juan", "x"))                 # lock del flash, con el par
    assert run(api.device_unlock("ttyUSB0", {"lock_user": "juan", "lock_token": "x"}))["ok"]
    last = [e for e in api_events() if e["type"] == "release"][-1]["detail"]
    assert last == {"user": "juan", "expires": None}


def test_forced_unlock_needs_the_api_token(tmux):
    """Sin token de la API, forzar un unlock es robar la reserva desde la red:
    403 force_disabled. Con token, exige el Bearer."""
    from server import locks
    board()
    reserve()
    with pytest.raises(HTTPException) as e:
        run(api.device_unlock("ttyUSB0", {"force": True}))
    assert err(e) == (403, "force_disabled") and locks.read("ttyUSB0") is not None
    assert run(api.get_version())["auth"] is False
    set_token("s3cret")
    assert run(api.get_version())["auth"] is True
    with pytest.raises(HTTPException) as e:
        run(api.device_unlock("ttyUSB0", {"force": True}))
    assert err(e) == (401, "auth")
    assert run(api.device_unlock("ttyUSB0", {"force": True}, authorization="Bearer s3cret"))["forced"]


def test_devremote_reset_records_event_with_who_forced(tmux):
    board()
    reserve()
    req = types.SimpleNamespace(client=types.SimpleNamespace(host="10.0.0.3"))
    r = run(api.devremote_reset("ttyUSB0", {"force": True, "lock_user": "dash"}, request=req))
    assert r["ok"]
    (ev,) = [e for e in api_events() if e["type"] == "command"]
    assert ev["detail"] == {"command": "restart-session", "user": "dash", "forced": True,
                            "by_user": "dash", "by_host": "10.0.0.3"}
    assert ev["cursor"].startswith(f"c:{SID}:")
    run(api.devremote_reset("ttyUSB0", {"lock_user": "alejo", "lock_token": "t0k"}))       # el dueño: sin forced
    assert [e for e in api_events() if e["type"] == "command"][-1]["detail"] == \
        {"command": "restart-session", "user": "alejo"}


def test_send_tmux_session_missing_is_session_down(monkeypatch):
    board()
    monkeypatch.setattr(api.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(
        returncode=1, stdout="", stderr="can't find session: esp32_ttyUSB0"))
    with pytest.raises(HTTPException) as e:
        run(api.device_send("ttyUSB0", {"text": "x"}))
    assert err(e) == (502, "session_down") and "restart-session" in e.value.detail["message"]


def test_reservation_lost_message_says_why():
    """Antes: "la reserva ya no es tuya (la tiene nadie)" también cuando venció."""
    board()
    owner = {"lock_user": "alejo", "lock_token": "t0k", "require_reservation": True}
    cases = []
    for setup in (lambda: None,
                  lambda: locks.write("ttyUSB0", locks.Lock("juan", "j", int(time.time()) + 600)),
                  lambda: locks.write("ttyUSB0", locks.Lock("alejo", "otro", int(time.time()) + 600)),
                  lambda: locks.write("ttyUSB0", locks.Lock("juan", "j"))):
        locks.remove("ttyUSB0")
        setup()
        with pytest.raises(HTTPException) as e:
            api._check_reservation("ttyUSB0", owner)
        assert err(e) == (423, "reservation_lost")
        cases.append(e.value.detail["message"])
    assert "venció" in cases[0] and "espbench reserve" not in cases[0]     # la sugerencia la agrega el CLI
    assert "la tiene 'juan' hasta" in cases[1]
    assert "otro lock_token" in cases[2]
    assert "lock de flash de 'juan'" in cases[3]
    assert not any("nadie" in m for m in cases)


def test_slow_subprocess_does_not_block_the_event_loop(monkeypatch):
    """devremote --reset tarda segundos: corrido directo en el handler async
    frenaba el event loop (todos los pedidos y el WebSocket del vivo)."""
    board()
    monkeypatch.setattr(api.subprocess, "run", lambda cmd, **kw: (time.sleep(0.4), types.SimpleNamespace(
        returncode=0, stdout="", stderr=""))[1])
    ticks = []

    async def ticker():
        for _ in range(15):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)

    async def both(handler):
        ticks.clear()
        await asyncio.gather(handler, ticker())
        return max(b - a for a, b in zip(ticks, ticks[1:]))

    assert run(both(api.devremote_reset("ttyUSB0"))) < 0.2
    assert run(both(api.device_send("ttyUSB0", {"text": "x"}))) < 0.2
    assert run(both(api.device_command("ttyUSB0", "reset"))) < 0.2


def test_devremote_reset_respects_reservations(tmux):
    """restart-session mata el proceso de la placa: una reserva ajena lo bloquea
    como a send/command."""
    board()
    paths.lock_file("ttyUSB0").parent.mkdir(parents=True, exist_ok=True)
    locks.write("ttyUSB0", locks.Lock("juan", "j", int(time.time()) + 600, "AABBCCDDEEFF"))
    with pytest.raises(HTTPException) as e:
        run(api.devremote_reset("ttyUSB0"))
    assert err(e) == (423, "locked") and tmux == []
    with pytest.raises(HTTPException) as e:
        run(api.devremote_reset("ttyUSB0", {"lock_user": "alejo", "lock_token": "t0k",
                                            "require_reservation": True}))
    assert err(e) == (423, "reservation_lost")
    with pytest.raises(HTTPException) as e:
        run(api.devremote_reset("ttyUSB0", {"expect_mac": "11:22:33:44:55:66", "force": True}))
    assert err(e) == (409, "device_changed")
    assert run(api.devremote_reset("ttyUSB0", {"lock_user": "juan", "lock_token": "j"}))["ok"]
    assert run(api.devremote_reset("ttyUSB0", {"force": True}))["ok"]
    assert tmux[-1] == ["/usr/local/bin/devremote", "--reset", "ttyUSB0"]


def test_version_identifies_bench(monkeypatch):
    monkeypatch.setattr(api.socket, "gethostname", lambda: "sensipi02")
    paths.version_file().parent.mkdir(parents=True, exist_ok=True)
    paths.version_file().write_text("0.13.0\n")
    assert run(api.get_version()) == {"app": "espbench", "version": "0.13.0", "name": "sensipi02", "auth": False}
    paths.bench_name_file().write_text("lab-cordoba\n")
    assert run(api.get_version())["name"] == "lab-cordoba"


def test_version_without_files():
    r = run(api.get_version())
    assert r["app"] == "espbench" and r["version"] == "dev" and r["name"]


# ---------- update del bench (espbench-update) ----------

def test_get_update_status_and_pin():
    r = run(api.get_update())
    assert r["pin"] is None and r["status"] is None
    paths.update_conf_file().parent.mkdir(parents=True, exist_ok=True)
    paths.update_conf_file().write_text("REPO_DIR=/opt/espbench\nPIN=feat/x\n")
    paths.update_status_file().write_text('{"state": "ok", "target": "feat/x"}')
    r = run(api.get_update())
    assert r["pin"] == "feat/x" and r["status"] == {"state": "ok", "target": "feat/x"}
    paths.update_conf_file().write_text("REPO_DIR=/opt/espbench\nPIN=\n")
    assert run(api.get_update())["pin"] is None


def test_post_update_launches_unit_outside_dashboard(tmux):
    r = run(api.post_update({"ref": "feat/bench-master"}))
    assert r["ok"] and r["ref"] == "feat/bench-master"
    cmd = tmux[-1]
    assert cmd[:4] == ["sudo", "-n", "systemd-run", f"--unit={r['unit']}"] and "--no-block" in cmd
    assert cmd[-3:] == ["/usr/local/bin/espbench-update", "--ref", "feat/bench-master"]
    run(api.post_update({}))
    assert tmux[-1][-1] == "--release"
    run(api.post_update({"force": True}))
    assert tmux[-1][-2:] == ["--release", "--force"]


@pytest.mark.parametrize("ref", ["--release", "-x", "a b", "../x", "a;rm", "x" * 101, "rama..mala"])
def test_post_update_rejects_bad_refs(tmux, ref):
    with pytest.raises(HTTPException) as e:
        run(api.post_update({"ref": ref}))
    assert err(e) == (400, "bad_request") and tmux == []


def test_post_update_busy_or_already_running(tmux):
    runstate.write("ttyUSB0", {"state": "flashing", "pid": os.getpid()})
    with pytest.raises(HTTPException) as e:
        run(api.post_update({}))
    assert err(e) == (409, "busy") and "ttyUSB0 flashing" in e.value.detail["message"] and tmux == []
    assert run(api.post_update({"force": True}))["ok"]
    runstate.remove("ttyUSB0")
    paths.update_status_file().write_text('{"state": "running", "message": "instalando v1.0.0"}')
    with pytest.raises(HTTPException) as e:
        run(api.post_update({"force": True}))
    assert err(e) == (409, "busy")


def test_post_update_needs_token_and_reports_launch_failure(tmux, monkeypatch):
    set_token("s3cret")
    with pytest.raises(HTTPException) as e:
        run(api.post_update({}))
    assert err(e) == (401, "auth") and tmux == []
    monkeypatch.setattr(api.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(
        returncode=1, stdout="", stderr="sudo: a password is required"))
    with pytest.raises(HTTPException) as e:
        run(api.post_update({}, authorization="Bearer s3cret"))
    assert err(e) == (502, "update_unavailable") and "password" in e.value.detail["message"]


# ---------- nota y propiedades por placa (PATCH /api/devices/{mac}, /api/properties) ----------

def test_patch_note_and_props_records_events_and_shows_in_devices():
    log = board()
    registered("mi-placa")
    req = types.SimpleNamespace(client=types.SimpleNamespace(host="10.0.0.9"))
    r = run(api.patch_device(MAC, {"note": " testeando, no tocar ", "user": "alejo",
                                   "props": {"chip": "esp32-s3", "estado": "no-tocar"},
                                   "props_add": {"conectividad": ["LTE"]}}, request=req))
    assert (r["note"], r["note_by"]) == ("testeando, no tocar", "alejo")
    assert r["props"] == {"chip": "esp32-s3", "estado": "no-tocar", "conectividad": ["lte"]}
    assert r["note_at"][-6] in "+-"                                  # ISO con la zona de la Pi
    evs = [e for e in api_events() if e["type"] in ("note", "props")]
    assert [(e["type"], e["detail"]) for e in evs] == [
        ("note", {"text": "testeando, no tocar", "user": "alejo"}),
        ("props", {"changes": {"chip": {"from": None, "to": "esp32-s3"}, "estado": {"from": None, "to": "no-tocar"},
                               "conectividad": {"from": None, "to": ["lte"]}}, "user": "alejo"})]
    assert evs[0]["cursor"].startswith("c:" + SID)
    from server.device_registry import DeviceRegistry
    d = DeviceRegistry(dev_dir=str(log.parent))._build_device_info("ttyUSB0")
    assert (d.note, d.note_by, d.props["chip"]) == ("testeando, no tocar", "alejo", "esp32-s3")
    # sin user: el host del pedido; nota vacía la borra; props_remove
    r = run(api.patch_device(MAC, {"note": "", "props_remove": {"conectividad": "lte"}}, request=req))
    assert r["note"] is None and "conectividad" not in r["props"]
    assert api_events()[-1]["detail"]["user"] == "10.0.0.9"
    n = len(api_events())
    run(api.patch_device(MAC, {"props": {"chip": "esp32-s3"}}))      # sin cambios: sin evento
    assert len(api_events()) == n


@pytest.mark.parametrize("body,msg", [
    ({"note": "a\nb"}, "control"), ({"note": "x" * 201}, "200"), ({"props": {"chip": "esp32-s4"}}, "esp32-s3"),
    ({"props": {"color": "x"}}, "categoría"), ({"user": "a\tb", "note": "x"}, "user"),
    ({"device_key": ""}, "device_key"), ({"props_add": {"chip": ["esp32", "esp32-c3"]}}, "un solo"),
])
def test_patch_rejects_bad_meta(body, msg):
    board()
    registered()
    with pytest.raises(HTTPException) as e:
        run(api.patch_device(MAC, body))
    assert err(e) == (400, "bad_request") and msg in e.value.detail["message"]


def test_patch_meta_unknown_board_is_404():
    with pytest.raises(HTTPException) as e:
        run(api.patch_device("11:22:33:44:55:66", {"note": "x"}))
    assert err(e) == (404, "not_found")


def test_patch_rename_still_works_and_meta_needs_token(tmux):
    registered()
    assert run(api.patch_device(MAC, {"device_key": "nuevo"})) == {"ok": True}
    set_token("s3cret")
    with pytest.raises(HTTPException) as e:
        run(api.patch_device(MAC, {"note": "x"}))
    assert err(e) == (401, "auth")
    assert run(api.patch_device(MAC, {"note": "x"}, authorization="Bearer s3cret"))["note"] == "x"
    for call in (lambda a: api.add_property_value("uso", {"id": "x"}, authorization=a),
                 lambda a: api.delete_property_value("uso", "ci", authorization=a)):
        with pytest.raises(HTTPException) as e:
            run(call(None))
        assert err(e) == (401, "auth")


def test_properties_endpoints():
    cats = run(api.get_properties())["categories"]
    assert [c["id"] for c in cats] == ["estado", "uso", "chip", "conectividad", "perifericos"]
    r = run(api.add_property_value("chip", {"id": "esp32-p4", "desc": "P4"}))
    assert r["value"] == {"id": "esp32-p4", "label": "esp32-p4", "desc": "P4"}
    with pytest.raises(HTTPException) as e:
        run(api.add_property_value("chip", {"id": "esp32-p4"}))
    assert err(e) == (400, "bad_request")
    registered("mi-placa")
    run(api.patch_device(MAC, {"props": {"chip": "esp32-p4"}}))
    with pytest.raises(HTTPException) as e:
        run(api.delete_property_value("chip", "esp32-p4"))
    assert err(e) == (409, "in_use") and "mi-placa" in e.value.detail["message"]
    run(api.patch_device(MAC, {"props": {"chip": None}}))
    assert run(api.delete_property_value("chip", "esp32-p4"))["ok"]
    with pytest.raises(HTTPException) as e:
        run(api.delete_property_value("chip", "esp32-p4"))
    assert err(e) == (404, "not_found")


def test_patch_with_corrupt_devices_json_is_a_handled_error_and_does_not_overwrite():
    registered()
    paths.devices_file().write_text('{"AA:BB": {roto')
    with pytest.raises(HTTPException) as e:
        run(api.patch_device(MAC, {"note": "x"}))
    assert err(e) == (500, "unexpected") and "ilegible" in e.value.detail["message"]
    assert paths.devices_file().read_text() == '{"AA:BB": {roto'


def test_note_by_without_user_says_it_came_from_the_dashboard():
    registered()
    req = types.SimpleNamespace(client=types.SimpleNamespace(host="10.0.0.9"))
    assert run(api.patch_device(MAC, {"note": "x", "via": "dashboard"}, request=req))["note_by"] == "dashboard@10.0.0.9"
    assert run(api.patch_device(MAC, {"note": "y", "via": "Mal Via!"}, request=req))["note_by"] == "10.0.0.9"


def test_delete_value_checks_in_use_under_the_devices_lock(monkeypatch):
    """La baja decide "en uso" con los datos leídos bajo el flock de devices.json."""
    from server.device_registry import DevicesFile
    registered()
    run(api.add_property_value("chip", {"id": "esp32-p4"}))
    seen = []
    orig = DevicesFile.locked

    def spy(self, fn):
        seen.append("locked")
        return orig(self, fn)
    monkeypatch.setattr(DevicesFile, "locked", spy)
    run(api.delete_property_value("chip", "esp32-p4"))
    assert seen == ["locked"]

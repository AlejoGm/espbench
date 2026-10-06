"""Tests de los endpoints de historial y consola de api.py. Sin TestClient (no
hay httpx): se llaman los handlers directo."""
import asyncio
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from fastapi import HTTPException

from server import api, paths, runstate

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
    for p in ("/api/device/{tty}/jobs", "/api/device/{tty}/sessions", "/api/device/{tty}/send"):
        assert paths_in_order.index(p) < catch_all


def test_send_without_tmux_is_502(monkeypatch):
    def no_tmux(cmd, **kw):
        raise FileNotFoundError("tmux")
    monkeypatch.setattr(api.subprocess, "run", no_tmux)
    with pytest.raises(HTTPException) as e:
        run(api.device_send("ttyUSB0", {"text": "x"}))
    assert e.value.status_code == 502


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
    assert r["message"] == "no estaba bloqueado" and not paths.lock_file("ttyUSB0").exists()


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
    assert abs(lock.expires - (time.time() + 600)) < 5 and r["expires"] == lock.expires_iso()
    (ev,) = api_events()
    assert ev["type"] == "reserve" and ev["detail"]["user"] == "alejo" and ev["cursor"].startswith(f"c:{SID}:")


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
    {"lock_user": "alejo", "lock_token": "a:b"},
    {"lock_user": "alejo", "lock_token": "t", "ttl_s": 0},
    {"lock_user": "alejo", "lock_token": "t", "ttl_s": "x"},
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

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

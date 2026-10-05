"""Tests de history: jobs con/sin result.json, sesiones de log, validación de nombres."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server import history, paths, runstate

MAC = "AA:BB:CC:DD:EE:FF"


def make_job(name, result=None, log="esptool ok\n", legacy=False):
    d = (paths.jobs_dir() if legacy else paths.device_jobs_dir(MAC)) / name
    d.mkdir(parents=True)
    if log is not None:
        (d / "job.log").write_text(log)
    if result is not None:
        (d / "result.json").write_text(json.dumps(result))
    return d


def test_list_jobs_newest_first_with_result():
    make_job("job_20261005_100000_b1", {"ok": True, "status": "exitoso", "user": "alejo"})
    make_job("job_20261005_120000_b1", {"ok": False, "status": "fallido", "error": "device_changed"})
    make_job("job_20261004_090000_b1_ttyUSB0", legacy=True)
    jobs = history.list_jobs("ttyUSB0", MAC)
    assert [j["job_id"] for j in jobs] == ["job_20261005_120000_b1", "job_20261005_100000_b1",
                                          "job_20261004_090000_b1_ttyUSB0"]
    assert jobs[0]["ok"] is False and jobs[0]["error"] == "device_changed"
    assert jobs[1]["ok"] is True and jobs[1]["user"] == "alejo" and jobs[1]["ts"] == "2026-10-05T10:00:00"
    assert jobs[2]["ok"] is None and jobs[2]["has_log"]


def test_list_jobs_ignores_other_ttys_legacy_and_limit():
    make_job("job_20261004_090000_b1_ttyUSB1", legacy=True)
    for i in range(5):
        make_job(f"job_20261005_10000{i}_b1", {"ok": True})
    assert all("ttyUSB1" not in j["job_id"] for j in history.list_jobs("ttyUSB0", MAC))
    assert len(history.list_jobs("ttyUSB0", MAC, limit=3)) == 3


def test_job_log_and_bad_names():
    make_job("job_20261005_100000_b1", log="linea de esptool\n")
    assert "linea de esptool" in history.job_log("ttyUSB0", MAC, "job_20261005_100000_b1")
    assert history.job_log("ttyUSB0", MAC, "job_inexistente") is None
    assert history.job_log("ttyUSB0", MAC, "../../etc/passwd") is None


def _make_sessions():
    home = paths.device_home(MAC)
    home.mkdir(parents=True)
    (home / "output.log").write_text("sesion actual\n")
    (home / "output_20261005_100000.log").write_text("sesion vieja\n")
    (home / "otra_cosa.txt").write_text("x")
    return home


def test_sessions_current_first():
    _make_sessions()
    names = [s["name"] for s in history.list_sessions("ttyUSB0", MAC)]
    assert names == ["output.log", "output_20261005_100000.log"]
    assert history.read_session("ttyUSB0", MAC, "output_20261005_100000.log") == "sesion vieja\n"


def test_sessions_follow_runstate_log_path():
    """Device sin MAC: el log está en el hogar provisorio que dice run/<tty>.json."""
    home = paths.device_unknown_home("ttyUSB0")
    home.mkdir(parents=True)
    (home / "output.log").write_text("sin mac\n")
    runstate.write("ttyUSB0", {"log_path": str(home / "output.log")})
    assert [s["name"] for s in history.list_sessions("ttyUSB0", None)] == ["output.log"]


def test_session_rejects_traversal():
    _make_sessions()
    for bad in ("../devices.json", "otra_cosa.txt", "output_x.log", "/etc/passwd"):
        assert history.session_path("ttyUSB0", MAC, bad) is None


def test_read_returns_tail_of_big_log(monkeypatch):
    home = _make_sessions()
    (home / "output.log").write_text("A" * 100 + "FINAL")
    monkeypatch.setattr(history, "MAX_READ", 10)
    assert history.read_session("ttyUSB0", MAC, "output.log") == "AAAAAFINAL"

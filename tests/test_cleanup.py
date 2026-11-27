"""cleanup.py: qué se borra (sesiones rotadas y jobs viejos), qué queda siempre (output.log, último job de cada
placa, events.jsonl, current.elf), CLI y endpoints."""
import asyncio
import json
import os
import pathlib
import sys
import time
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from fastapi import HTTPException

from server import api, cleanup, paths

MAC = "AABBCCDDEEFF"
DAY = 86400
NOW = time.time()


def put(p: pathlib.Path, size: int, age_days: float) -> pathlib.Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    t = NOW - age_days * DAY
    os.utime(p, (t, t))
    return p


def job(jobs: pathlib.Path, name: str, age_days: float, size: int = 1000) -> pathlib.Path:
    d = jobs / name
    put(d / "artifact.zip", size, age_days)
    put(d / "job.log", 10, age_days)
    t = NOW - age_days * DAY
    os.utime(d, (t, t))
    return d


@pytest.fixture
def bench():
    home = paths.device_home(MAC)
    b = types.SimpleNamespace(home=home)
    b.current = put(home / "output.log", 500, 30)                 # sesión en curso: nunca
    b.old_sess = put(home / "output_20261001_100000_12.log", 200, 30)
    b.new_sess = put(home / "output_20261120_100000_12.log", 300, 2)
    b.events = put(home / "events.jsonl", 50, 30)
    b.elf = put(home / "current.elf", 50, 30)
    b.other = put(home / "notas.log", 50, 30)                     # no es una sesión
    jobs = paths.device_jobs_dir(MAC)
    b.job_old = job(jobs, "job_20261001_100000", 40)
    b.job_old2 = job(jobs, "job_20261002_100000", 39)
    b.job_last = job(jobs, "job_20261003_100000", 38)              # el último de la placa: queda aunque sea viejo
    b.unknown_sess = put(paths.device_unknown_home("ttyUSB3") / "output_20261001_100000_9.log", 70, 30)
    legacy = paths.jobs_dir()
    b.leg_old = job(legacy, "job_20260901_100000_ttyUSB0", 80)
    b.leg_last = job(legacy, "job_20260902_100000_ttyUSB0", 79)
    b.leg_other = job(legacy, "job_20260901_100000_ttyUSB1", 80)   # único de ttyUSB1: queda
    return b


def paths_of(items):
    return {pathlib.Path(i["path"]) for i in items}


def test_plan_picks_old_sessions_and_jobs_but_keeps_last_job(bench):
    items = cleanup.plan(14, now=NOW)
    assert paths_of(items) == {bench.old_sess, bench.unknown_sess, bench.job_old, bench.job_old2, bench.leg_old}
    s = cleanup.summary(items, 14)
    assert s["sessions"] == {"count": 2, "bytes": 270}
    assert s["jobs"] == {"count": 3, "bytes": 3 * 1010}
    assert s["total_bytes"] == 270 + 3030
    assert bench.current.exists()      # plan solo lee


def test_plan_age_threshold(bench):
    assert paths_of(cleanup.plan(1, now=NOW)) >= {bench.new_sess}
    assert bench.new_sess not in paths_of(cleanup.plan(3, now=NOW))
    assert paths_of(cleanup.plan(60, now=NOW)) == {bench.leg_old}


def test_job_in_progress_counts_by_its_newest_file(bench):
    """Un job viejo con un archivo recién escrito adentro no es viejo."""
    put(bench.job_old / "job.log", 10, 0)
    assert bench.job_old not in paths_of(cleanup.plan(14, now=NOW))


def test_run_deletes_and_keeps_the_rest(bench):
    res = cleanup.run(14, now=NOW)
    assert res["freed_bytes"] == 3300 and res["errors"] == []
    for p in (bench.old_sess, bench.unknown_sess, bench.job_old, bench.job_old2, bench.leg_old):
        assert not p.exists()
    for p in (bench.current, bench.new_sess, bench.events, bench.elf, bench.other,
              bench.job_last, bench.leg_last, bench.leg_other):
        assert p.exists()
    assert cleanup.run(14, now=NOW)["freed_bytes"] == 0


def test_symlinks_are_not_followed(bench, tmp_path):
    outside = tmp_path / "fuera"
    put(outside / "importante.bin", 10, 100)
    os.utime(outside, (NOW - 100 * DAY,) * 2)
    (paths.device_jobs_dir(MAC) / "job_20260101_000000").symlink_to(outside)
    (bench.home / "output_20260101_000000_1.log").symlink_to(outside / "importante.bin")
    cleanup.run(14, now=NOW)
    assert (outside / "importante.bin").exists()


def test_apply_refuses_paths_outside_base(tmp_path):
    f = put(tmp_path / "otro" / "output_1.log", 10, 100)
    done, errors = cleanup.apply([{"path": str(f), "kind": "session", "bytes": 10, "mtime": 0}])
    assert done == [] and errors[0]["error"] == "fuera de ESP_BASE" and f.exists()


@pytest.mark.parametrize("days", [0, -1, cleanup.MAX_DAYS + 1])
def test_plan_rejects_bad_days(days):
    with pytest.raises(ValueError):
        cleanup.plan(days)


def test_no_devices_dir_is_empty():
    assert cleanup.plan(14) == []


def test_cli_json_and_dry_run(bench, capsys, monkeypatch):
    base = str(paths.esp_base())
    monkeypatch.delenv("ESP_BASE")
    assert cleanup.main(["--days", "14", "--dry-run", "--json", "--base", base]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sessions"]["count"] == 2 and bench.old_sess.exists()
    assert cleanup.main(["--days", "14", "--json", "--base", base]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["freed_bytes"] == 3300 and not bench.old_sess.exists()
    assert cleanup.main(["--days", "0", "--json"]) == 2


# ---------- endpoints ----------

def run(coro):
    return asyncio.run(coro)


def err(e):
    return e.value.status_code, e.value.detail["error"]


def test_get_cleanup_preview(bench):
    r = run(api.get_cleanup(14))
    assert r["days"] == 14 and r["sessions"]["count"] == 2 and r["jobs"]["count"] == 3
    assert bench.old_sess.exists()
    with pytest.raises(HTTPException) as e:
        run(api.get_cleanup(0))
    assert err(e) == (400, "bad_request")


def test_post_cleanup_runs_cli_as_root(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = {"days": 7, "sessions": {"count": 1, "bytes": 5}, "jobs": {"count": 0, "bytes": 0},
               "freed_bytes": 5, "errors": []}
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")
    monkeypatch.setattr(api.subprocess, "run", fake_run)
    r = run(api.post_cleanup({"days": 7}))
    assert r["ok"] and r["freed_bytes"] == 5
    cmd = calls[-1]
    assert cmd[:3] == ["sudo", "-n", sys.executable] and cmd[3:5] == ["-m", "server.cleanup"]
    assert cmd[cmd.index("--days") + 1] == "7" and "--json" in cmd
    assert cmd[cmd.index("--base") + 1] == str(paths.esp_base())
    run(api.post_cleanup(None))
    assert calls[-1][calls[-1].index("--days") + 1] == str(cleanup.DEFAULT_DAYS)


@pytest.mark.parametrize("days", [0, "x", None, cleanup.MAX_DAYS + 1])
def test_post_cleanup_rejects_bad_days(monkeypatch, days):
    monkeypatch.setattr(api.subprocess, "run", lambda *a, **k: pytest.fail("no debería correr"))
    with pytest.raises(HTTPException) as e:
        run(api.post_cleanup({"days": days}))
    assert err(e) == (400, "bad_request")


def test_post_cleanup_needs_token_and_reports_failure(monkeypatch):
    paths.api_token_file().parent.mkdir(parents=True, exist_ok=True)
    paths.api_token_file().write_text("s3cret\n")
    os.chmod(paths.api_token_file(), 0o600)
    monkeypatch.setattr(api.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(
        returncode=1, stdout="", stderr="sudo: a password is required"))
    with pytest.raises(HTTPException) as e:
        run(api.post_cleanup({}))
    assert err(e) == (401, "auth")
    with pytest.raises(HTTPException) as e:
        run(api.post_cleanup({}, authorization="Bearer s3cret"))
    assert err(e) == (502, "cleanup_unavailable") and "password" in e.value.detail["message"]

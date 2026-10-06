"""CLI `espbench` por subprocess contra la Pi simulada (tests/benchsim.py, en
este proceso): exit codes del contrato (docs/specs/agents-cli.md §8.3) y forma
del JSON (un objeto por comando)."""
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client import espbench_lib as lib  # noqa: E402
from tests.benchsim import Bench  # noqa: E402
from tests.test_espbench_lib import make_build, make_client  # noqa: E402

CLI = ROOT / "client" / "espbench.py"


@pytest.fixture
def bench():
    with Bench() as b:
        b.add_board(key="sim-board")
        yield b


@pytest.fixture
def board(bench):
    return bench.boards["ttyUSB0"]


def cli(bench, tmp_path, *args, as_json=True, user="agent", host=None, timeout=60):
    env = {**os.environ, "HOME": str(tmp_path), "ESPBENCH_HOST": host or bench.host, "ESPBENCH_USER": user,
           "ESPBENCH_LOCK_TOKEN": "t0k", "ESPBENCH_CONFIG": str(tmp_path / "no-config.json"),
           "ESPBENCH_STATE_DIR": str(tmp_path / f"state-{user}")}
    env.pop("ESPBENCH_TOKEN", None)
    env.pop("ESPBENCH_PROFILE", None)
    argv = [sys.executable, str(CLI), *args] + (["--json"] if as_json else [])
    p = subprocess.run(argv, cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=timeout)
    if not as_json:
        return p.returncode, p.stdout, p.stderr
    lines = p.stdout.strip().splitlines()
    assert len(lines) == 1, (p.stdout, p.stderr)          # un objeto JSON por comando
    return p.returncode, json.loads(lines[0])


def test_ls_and_common_flags_before_or_after(bench, tmp_path):
    code, r = cli(bench, tmp_path, "ls")
    assert code == 0 and r["ok"]
    (d,) = r["devices"]
    assert (d["key"], d["tty"], d["mac"], d["state"], d["fw_project"]) == \
        ("sim-board", "ttyUSB0", "AA:BB:CC:DD:EE:01", "monitoring", "simfw")
    code, r2 = cli(bench, tmp_path, "--json", "ls", as_json=False)[:2]
    assert code == 0 and json.loads(r2)["devices"][0]["key"] == "sim-board"


def test_agent_cycle(bench, board, tmp_path):
    """reserve → who → send --until idle → panic → logs --around → events → release."""
    code, r = cli(bench, tmp_path, "reserve", "sim-board", "--ttl", "10m")
    assert code == 0 and r["user"] == "agent" and r["expires"]
    code, r = cli(bench, tmp_path, "who", "sim-board")
    assert code == 0 and r["mine"] and r["reservation"] and r["lock_user"] == "agent"
    code, r = cli(bench, tmp_path, "send", "sim-board", "status", "--until", "idle:300ms")
    assert code == 0 and r["reason"] == "idle" and r["cursor"].startswith("c:")
    assert any(l.endswith("OK uptime=12s heap=210000") for l in r["lines"])
    code, r = cli(bench, tmp_path, "send", "sim-board", "panic", "--until", "panic")
    assert code == 0 and "Guru Meditation" in r["match"]
    time.sleep(0.3)
    code, r = cli(bench, tmp_path, "logs", "sim-board", "--around", "panic", "--max-lines", "50")
    assert code == 0 and r["reason"] == "range" and any("Backtrace" in l for l in r["lines"])
    code, r = cli(bench, tmp_path, "events", "sim-board", "--type", "panic")
    assert code == 0 and [e["type"] for e in r["events"]] == ["panic"] and r["session"]
    code, r = cli(bench, tmp_path, "status", "sim-board")
    assert code == 0 and r["health"]["panics"] == 1 and r["lock_user"] == "agent" and r["events"]
    code, r = cli(bench, tmp_path, "release", "sim-board")
    assert code == 0 and r["message"] == "liberado"


def test_events_all(bench, tmp_path):
    bench.add_board("ttyUSB1", "AA:BB:CC:DD:EE:02", key="otra")
    code, r = cli(bench, tmp_path, "events", "--all", "--type", "boot")
    assert code == 0 and sorted({e["board"] for e in r["events"]}) == ["otra", "sim-board"]


@pytest.mark.parametrize("args,code,error", [
    (("logs", "no-existe"), 7, "not_found"),
    (("send", "no-existe", "x"), 7, "not_found"),
    (("logs", "sim-board", "--since", "ayer"), 8, "bad_anchor"),
    (("logs", "sim-board", "--since", "c:20200101_000000_1:0"), 8, "cursor_expired"),
    (("logs", "sim-board", "--since", "now", "--until", "nunca", "--timeout", "0.5s"), 4, "timeout"),
    (("logs", "sim-board", "--grep", "("), 1, "bad_request"),
    (("reset", "sim-board", "--bootloader", "--verify"), 1, "bad_request"),
    (("logs", "sim-board", "--timeout", "diez", "--until", "x"), 1, "bad_request"),
    (("nada",), 1, "bad_request"),
])
def test_exit_codes(bench, tmp_path, args, code, error):
    got, r = cli(bench, tmp_path, *args)
    assert (got, r["ok"], r["error"]) == (code, False, error), r
    assert r["message"]


def test_crashed_and_expect_panic(bench, tmp_path):
    code, r = cli(bench, tmp_path, "send", "sim-board", "panic", "--until", "OK", "--timeout", "5s")
    assert code == 3 and r["error"] == "crashed" and r["crash"]["type"] == "panic"
    code, r = cli(bench, tmp_path, "send", "sim-board", "panic", "--until", "OK", "--timeout", "5s",
                  "--expect-panic")
    assert code == 0 and r["ok"] and r["reason"] == "panic"


def test_locked_and_reservation_lost(bench, tmp_path):
    assert cli(bench, tmp_path, "reserve", "sim-board", user="juan")[0] == 0
    code, r = cli(bench, tmp_path, "send", "sim-board", "status")
    assert (code, r["error"]) == (6, "locked")
    code, r = cli(bench, tmp_path, "reserve", "sim-board")
    assert (code, r["error"]) == (6, "locked")
    assert cli(bench, tmp_path, "release", "sim-board", user="juan")[0] == 0
    assert cli(bench, tmp_path, "reserve", "sim-board")[0] == 0
    # la suelta "otra máquina" con el mismo par: la próxima escritura se entera
    twin = make_client(bench, tmp_path, state="twin")
    twin.release(twin.resolve("sim-board", write=True))
    code, r = cli(bench, tmp_path, "reset", "sim-board")
    assert (code, r["error"]) == (6, "reservation_lost")


def test_busy(bench, board, tmp_path):
    board.device.start_flash()
    try:
        code, r = cli(bench, tmp_path, "send", "sim-board", "status")
        assert (code, r["error"]) == (5, "busy")
    finally:
        board.device.finish_flash()


def test_network(bench, tmp_path):
    code, r = cli(bench, tmp_path, "ls", host="127.0.0.1:1")
    assert (code, r["error"]) == (10, "network")


def write_project(tmp_path, **cfg):
    make_build(tmp_path)
    (tmp_path / ".flashcfg.json").write_text(json.dumps({"chip": "esp32", "encrypt": False, **cfg}))


def test_flash_verify(bench, board, tmp_path):
    write_project(tmp_path)
    code, r = cli(bench, tmp_path, "flash", "sim-board", "--verify=0.5s")
    assert code == 0 and r["ok"] and r["status"] == "exitoso" and r["cursor"].startswith("c:")
    assert r["verify"]["ok"] and "SW_CPU_RESET" in r["verify"]["boot"]
    assert board.flashes == 1


def test_flash_verify_new_session(bench, board, tmp_path):
    write_project(tmp_path)
    board.after_flash = board.replug_then_boot
    code, r = cli(bench, tmp_path, "flash", "sim-board", "--verify=0.5s")
    assert code == 0 and r["verify"]["new_session"], r


def test_flash_verify_crash_is_3(bench, board, tmp_path):
    write_project(tmp_path)
    board.after_flash = lambda: board.boot_then_panic(0.3)
    code, r = cli(bench, tmp_path, "flash", "sim-board", "--verify=1.5s")
    assert (code, r["error"]) == (3, "crashed") and r["verify"]["crash"]["type"] == "panic"
    assert r["status"] == "exitoso"                    # el flash anduvo; lo que falló es el arranque


def test_flash_failed_is_2(bench, board, tmp_path):
    write_project(tmp_path)
    board.flash_rc = 2
    code, r = cli(bench, tmp_path, "flash", "sim-board")
    assert (code, r["error"]) == (2, "flash_failed") and r["error_hint"] and r["log_tail"]


def test_flash_without_build_is_bad_request(bench, tmp_path):
    code, r = cli(bench, tmp_path, "flash", "sim-board", "--build-dir", "no-existe")
    assert (code, r["error"]) == (1, "bad_request") and "build" in r["message"]


def test_reset_verify(bench, tmp_path):
    code, r = cli(bench, tmp_path, "reset", "sim-board", "--verify=0.3s")
    assert code == 0 and r["command"] == "reset" and "RTCWDT_RTC_RESET" in r["verify"]["boot"]


def test_human_output(bench, tmp_path):
    code, out, err = cli(bench, tmp_path, "ls", as_json=False)
    assert code == 0 and out.splitlines()[0].split()[:3] == ["KEY", "SN", "MAC"] and "sim-board" in out
    code, out, err = cli(bench, tmp_path, "logs", "sim-board", "--since", "session", "--until", "boot",
                         as_json=False)
    assert code == 0 and "rst:0x1 (POWERON_RESET)" in out and "== until:" in err
    code, out, err = cli(bench, tmp_path, "logs", "no-existe", as_json=False)
    assert code == 7 and out == "" and err.startswith("error: not_found:")


def test_module_entrypoint():
    p = subprocess.run([sys.executable, "-m", "client.espbench", "--help"], cwd=str(ROOT), capture_output=True,
                       text=True, timeout=30)
    assert p.returncode == 0 and "restart-session" in p.stdout


def test_install_script_puts_espbench_in_bin(tmp_path):
    """install.sh deja un `espbench` ejecutable (idempotente). Sin pip: el CLI no
    tiene dependencias."""
    env = {**os.environ, "ESPBENCH_BIN_DIR": str(tmp_path / "bin"), "ESPBENCH_VENV": str(tmp_path / "venv"),
           "ESPBENCH_SKIP_PIP": "1"}
    for _ in range(2):
        p = subprocess.run(["bash", str(ROOT / "client" / "install.sh")], env=env, capture_output=True, text=True,
                           timeout=120)
        assert p.returncode == 0, p.stderr
    wrapper = tmp_path / "bin" / "espbench"
    assert os.access(wrapper, os.X_OK)
    p = subprocess.run([str(wrapper), "--help"], capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
    assert p.returncode == 0 and "restart-session" in p.stdout

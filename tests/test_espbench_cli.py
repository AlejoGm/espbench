"""CLI `espbench` contra la Pi simulada (tests/benchsim.py, en este proceso):
exit codes del contrato (docs/specs/agents-cli.md §8.3) y forma del JSON (un
objeto por comando). Casi todos llaman a main(argv) en el proceso (rápido); por
subprocess, el contrato sobre un proceso real, `python -m` e install.sh."""
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "remote"))

from client import espbench_lib as lib  # noqa: E402
from server import locks  # noqa: E402
from tests.benchsim import Bench  # noqa: E402
from client import espbench  # noqa: E402
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


class _FastClient(lib.Client):
    def __init__(self, *a, **kw):
        kw.setdefault("poll_s", 0.05)
        super().__init__(*a, **kw)


@pytest.fixture
def cli(bench, tmp_path, monkeypatch, capsys):
    """El CLI en este proceso (main(argv)), con su entorno: igual que por
    subprocess pero sin arrancar un intérprete por comando."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(lib, "Client", _FastClient)
    for k in ("ESPBENCH_TOKEN", "ESPBENCH_PROFILE"):
        monkeypatch.delenv(k, raising=False)

    def run(*args, as_json=True, user="agent", host=None):
        env = {"HOME": str(tmp_path), "ESPBENCH_HOST": host or bench.host, "ESPBENCH_USER": user,
               "ESPBENCH_LOCK_TOKEN": "t0k", "ESPBENCH_CONFIG": str(tmp_path / "no-config.json"),
               "ESPBENCH_STATE_DIR": str(tmp_path / f"state-{user}")}
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        capsys.readouterr()
        code = espbench.main(list(args) + (["--json"] if as_json else []))
        out, err = capsys.readouterr()
        if not as_json:
            return code, out, err
        lines = out.strip().splitlines()
        assert len(lines) == 1, (out, err)          # un objeto JSON por comando
        return code, json.loads(lines[0])
    return run


def subprocess_cli(bench, tmp_path, *args):
    env = {**os.environ, "HOME": str(tmp_path), "ESPBENCH_HOST": bench.host, "ESPBENCH_USER": "agent",
           "ESPBENCH_LOCK_TOKEN": "t0k", "ESPBENCH_CONFIG": str(tmp_path / "no-config.json"),
           "ESPBENCH_STATE_DIR": str(tmp_path / "state")}
    env.pop("ESPBENCH_TOKEN", None)
    p = subprocess.run([sys.executable, str(CLI), *args], cwd=str(tmp_path), env=env, capture_output=True,
                       text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def test_json_contract_over_a_real_process(bench, tmp_path):
    """Por subprocess: un objeto JSON en stdout, nada más, y el exit code."""
    code, out, err = subprocess_cli(bench, tmp_path, "ls", "--json")
    assert code == 0 and len(out.strip().splitlines()) == 1
    (d,) = json.loads(out)["devices"]
    assert (d["key"], d["tty"], d["mac"], d["state"], d["fw_project"]) == \
        ("sim-board", "ttyUSB0", "AA:BB:CC:DD:EE:01", "monitoring", "simfw")
    code, out, err = subprocess_cli(bench, tmp_path, "--json", "logs", "sim-board", "--since", "ayer")
    assert code == 8 and json.loads(out)["error"] == "bad_anchor"


def test_ls_marks_available_boards(cli, bench, board):
    """available: monitoring y sin lock, o con lock propio. El lock permanente
    de un flash ajeno no deja flashear ni reservar: no está disponible."""
    code, r = cli("ls")
    assert code == 0 and r["devices"][0]["available"] is True
    locks.write("ttyUSB0", locks.Lock("juan", "x"))                 # lo dejó el flash de otro
    assert cli("ls")[1]["devices"][0]["available"] is False
    assert cli("status", "sim-board")[1]["available"] is False
    locks.remove("ttyUSB0")
    assert cli("reserve", "sim-board")[0] == 0
    assert cli("ls")[1]["devices"][0]["available"] is True          # la reserva es mía
    assert cli("ls", user="otro")[1]["devices"][0]["available"] is False
    board.device.start_flash()
    assert cli("ls")[1]["devices"][0]["available"] is False         # flasheando
    board.device.finish_flash()


def test_common_flags_before_or_after(cli):
    code, r = cli("ls")
    assert code == 0 and r["devices"][0]["key"] == "sim-board"
    code, out, _ = cli("--json", "ls", as_json=False)
    assert code == 0 and json.loads(out)["devices"][0]["key"] == "sim-board"


def test_agent_cycle(cli, bench, board, tmp_path):
    """reserve → who → send --until idle → panic → logs --around → events → release."""
    code, r = cli("reserve", "sim-board", "--ttl", "10m")
    assert code == 0 and r["user"] == "agent" and r["expires"]
    code, r = cli("who", "sim-board")
    assert code == 0 and r["mine"] and r["reservation"] and r["lock_user"] == "agent"
    code, r = cli("send", "sim-board", "status", "--until", "idle:300ms")
    assert code == 0 and r["reason"] == "idle" and r["cursor"].startswith("c:")
    assert any(l.endswith("OK uptime=12s heap=210000") for l in r["lines"])
    code, r = cli("send", "sim-board", "panic", "--until", "panic")
    assert code == 0 and "Guru Meditation" in r["match"]
    time.sleep(0.3)
    code, r = cli("logs", "sim-board", "--around", "panic", "--max-lines", "50")
    assert code == 0 and r["reason"] == "range" and any("Backtrace" in l for l in r["lines"])
    code, r = cli("events", "sim-board", "--type", "panic")
    assert code == 0 and [e["type"] for e in r["events"]] == ["panic"] and r["session"]
    code, r = cli("status", "sim-board")
    assert code == 0 and r["health"]["panics"] == 1 and r["lock_user"] == "agent" and r["events"]
    code, r = cli("release", "sim-board")
    assert code == 0 and r["message"] == "liberado"


def test_events_all(cli, bench, tmp_path):
    bench.add_board("ttyUSB1", "AA:BB:CC:DD:EE:02", key="otra")
    code, r = cli("events", "--all", "--type", "boot")
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
def test_exit_codes(cli, bench, tmp_path, args, code, error):
    got, r = cli(*args)
    assert (got, r["ok"], r["error"]) == (code, False, error), r
    assert r["message"]


def test_crashed_and_expect_panic(cli, bench, tmp_path):
    code, r = cli("send", "sim-board", "panic", "--until", "OK", "--timeout", "5s")
    assert code == 3 and r["error"] == "crashed" and r["crash"]["type"] == "panic"
    code, r = cli("send", "sim-board", "panic", "--until", "OK", "--timeout", "5s",
                  "--expect-panic")
    assert code == 0 and r["ok"] and r["reason"] == "panic"


def test_locked_and_reservation_lost(cli, bench, tmp_path):
    assert cli("reserve", "sim-board", user="juan")[0] == 0
    code, r = cli("send", "sim-board", "status")
    assert (code, r["error"]) == (6, "locked")
    code, r = cli("reserve", "sim-board")
    assert (code, r["error"]) == (6, "locked")
    assert cli("release", "sim-board", user="juan")[0] == 0
    assert cli("reserve", "sim-board")[0] == 0
    # la suelta "otra máquina" con el mismo par: la próxima escritura se entera
    twin = make_client(bench, tmp_path, state="twin")
    twin.release(twin.resolve("sim-board", write=True))
    code, r = cli("reset", "sim-board")
    assert (code, r["error"]) == (6, "reservation_lost")


def test_busy(cli, bench, board, tmp_path):
    board.device.start_flash()
    try:
        code, r = cli("send", "sim-board", "status")
        assert (code, r["error"]) == (5, "busy")
    finally:
        board.device.finish_flash()


def test_network(cli, bench, tmp_path):
    code, r = cli("ls", host="127.0.0.1:1")
    assert (code, r["error"]) == (10, "network")


def write_project(tmp_path, **cfg):
    make_build(tmp_path)
    (tmp_path / ".flashcfg.json").write_text(json.dumps({"chip": "esp32", "encrypt": False, **cfg}))


def test_flash_verify(cli, bench, board, tmp_path):
    write_project(tmp_path)
    code, r = cli("flash", "sim-board", "--verify=0.5s")
    assert code == 0 and r["ok"] and r["status"] == "exitoso" and r["cursor"].startswith("c:")
    assert r["verify"]["ok"] and "SW_CPU_RESET" in r["verify"]["boot"]
    assert "lines" not in r["verify"] and "events" not in r["verify"]        # ok: alcanza con el boot
    assert board.flashes == 1


def test_flash_verify_new_session(cli, bench, board, tmp_path):
    write_project(tmp_path)
    board.after_flash = board.replug_then_boot
    code, r = cli("flash", "sim-board", "--verify=0.5s")
    assert code == 0 and r["verify"]["new_session"], r


def test_flash_verify_crash_is_3(cli, bench, board, tmp_path):
    write_project(tmp_path)
    board.after_flash = lambda: board.boot_then_panic(0.3)
    code, r = cli("flash", "sim-board", "--verify=1.5s")
    assert (code, r["error"]) == (3, "crashed") and r["verify"]["crash"]["type"] == "panic"
    assert r["status"] == "exitoso"                    # el flash anduvo; lo que falló es el arranque


def test_flash_failed_is_2(cli, bench, board, tmp_path):
    write_project(tmp_path)
    board.flash_rc = 2
    code, r = cli("flash", "sim-board")
    assert (code, r["error"]) == (2, "flash_failed") and r["error_hint"] and r["log_tail"]


def test_flash_without_build_is_bad_request(cli, bench, tmp_path):
    code, r = cli("flash", "sim-board", "--build-dir", "no-existe")
    assert (code, r["error"]) == (1, "bad_request") and "build" in r["message"]


def test_reset_verify(cli, bench, tmp_path):
    code, r = cli("reset", "sim-board", "--verify=0.3s")
    assert code == 0 and r["command"] == "reset" and "RTCWDT_RTC_RESET" in r["verify"]["boot"]


def test_flash_then_reset_verify_many_times_is_not_a_boot_loop(cli, bench, board, tmp_path):
    """C1: flash --verify y después varios `reset --verify` seguidos. Antes (3
    boots en 120 s = boot loop, y solo el flash ponía los contadores en cero) el
    segundo reset daba exit 3 `crashed` (boot_loop). Ahora un reset pedido al
    monitor (Ctrl-T Ctrl-R) también los pone en cero: 6 boots en ~10 s andan."""
    write_project(tmp_path)
    code, r = cli("flash", "sim-board", "--verify=0.3s")
    assert code == 0, r
    for i in range(5):
        code, r = cli("reset", "sim-board", "--verify=0.3s")
        assert code == 0 and r["verify"]["ok"], (i, r)
        assert "boot_loop" not in r["verify"]
    health = board.manager.watch.health()
    assert health["boots"] == 1 and not health["boot_loop"]


def test_human_output(cli, bench, tmp_path):
    code, out, err = cli("ls", as_json=False)
    assert code == 0 and out.splitlines()[0].split()[:3] == ["KEY", "SN", "MAC"] and "sim-board" in out
    code, out, err = cli("logs", "sim-board", "--since", "session", "--until", "boot",
                         as_json=False)
    assert code == 0 and "rst:0x1 (POWERON_RESET)" in out and "== until:" in err
    code, out, err = cli("logs", "no-existe", as_json=False)
    assert code == 7 and out == "" and err.startswith("error: not_found:")


@pytest.mark.parametrize("args", [
    ("send", "sim-board", "status", "--until", "re:("),
    ("send", "sim-board", "status", "--until", "x", "--timeout", "diez"),
    ("send", "sim-board", "status", "--until", "idle:abc"),
    ("send", "sim-board", "status", "--for", "mucho"),
    ("flash", "sim-board", "--verify=abc"),
    ("reset", "sim-board", "--verify=abc"),
    ("reserve", "sim-board", "--ttl", "nunca"),
])
def test_arguments_are_validated_before_writing(cli, bench, board, tmp_path, args):
    """Antes `send` mandaba y después fallaba por --timeout/regex, y
    `flash --verify=abc` flasheaba y recién ahí devolvía bad_request."""
    write_project(tmp_path)
    code, r = cli(*args)
    assert (code, r["error"]) == (1, "bad_request"), r
    assert board.flashes == 0 and not any(c[:2] == ["tmux", "send-keys"] for c in bench.run.calls)
    assert locks.read("ttyUSB0") is None


def test_error_after_writing_carries_what_was_written(cli, bench, tmp_path, monkeypatch):
    def down(self, *a, **k):
        raise lib.EspbenchError("network", "se cayó la Pi")
    monkeypatch.setattr(lib.Client, "read_range", down)
    code, r = cli("send", "sim-board", "status", "--until", "OK")
    assert (code, r["error"]) == (10, "network") and r["sent"] == "status" and r["cursor"].startswith("c:")
    monkeypatch.setattr(lib.Client, "verify", down)
    write_project(tmp_path)
    code, r = cli("flash", "sim-board", "--verify")
    assert (code, r["error"]) == (10, "network") and r["job_id"] and r["cursor"].startswith("c:")
    assert r["status"] == "exitoso"


def test_send_output_is_compact(cli, bench):
    assert cli("reserve", "sim-board")[0] == 0
    code, r = cli("send", "sim-board", "status", "--until", "OK")
    assert code == 0 and "start" not in r and "events" not in r and "session_ended" not in r


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

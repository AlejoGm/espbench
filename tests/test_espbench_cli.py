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
        env = {"HOME": str(tmp_path), "ESPBENCH_HOST": bench.host if host is None else host, "ESPBENCH_USER": user,
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


class _FakeBench:
    """Un bench por HTTP que solo contesta /api/version y /api/devices (para el discovery)."""

    def __init__(self, version: dict, devices: list):
        import http.server
        import threading
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = {"/api/version": fake.version, "/api/devices": fake.devices}.get(self.path)
                data = json.dumps(body if body is not None else {"detail": "Not Found"}).encode()
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.version, self.devices = version, devices
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.host = f"127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


OTHER = {"tty_name": "ttyUSB3", "tty": "/dev/ttyUSB3", "port_tcp": 5003, "mac": "AA:BB:CC:DD:EE:99",
         "sn": "SFY00099", "device_key": "otra-placa", "state": "monitoring", "status": "RUNNING",
         "lock_user": None}


@pytest.fixture
def benches_net(bench, tmp_path, monkeypatch):
    """Discovery sin red real: el bench simulado ("bench-sim"), otro bench que solo lista
    placas ("bench-b") y uno viejo (solo {"version"}), por ESPBENCH_BENCHES_CONFIG."""
    from server import paths
    paths.bench_name_file().parent.mkdir(parents=True, exist_ok=True)
    paths.bench_name_file().write_text("bench-sim\n")
    b = _FakeBench({"app": "espbench", "version": "0.34.0", "name": "bench-b", "location": "Lab Chile",
                    "auth": True}, [dict(OTHER)])
    old = _FakeBench({"version": "0.6.0"}, [dict(OTHER, device_key="vieja")])
    cfg = tmp_path / "benches.json"
    cfg.write_text(json.dumps({"tailscale": False, "hosts": [bench.host, b.host, old.host], "timeout_s": 2}))
    monkeypatch.setenv("ESPBENCH_BENCHES_CONFIG", str(cfg))
    yield {"b": b, "old": old}
    b.close()
    old.close()


@pytest.fixture
def disc(cli, benches_net):
    """El CLI sin host (ESPBENCH_HOST vacío: discovery)."""
    def run(*args, **kw):
        return cli(*args, host="", **kw)
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


# ---------- discovery: sin host, los benches se encuentran solos ----------

def test_benches_lists_found_benches(disc, board):
    code, r = disc("benches")
    assert code == 0
    by = {b["name"]: b for b in r["benches"]}
    assert set(by) == {"bench-sim", "bench-b", "127.0.0.1"}
    assert (by["bench-sim"]["boards"], by["bench-sim"]["available"], by["bench-sim"]["supported"]) == (1, 1, True)
    assert by["bench-b"]["auth"] is True and by["bench-b"]["version"] == "0.34.0"
    old = by["127.0.0.1"]
    assert old["supported"] is False and old["version"] == "0.6.0"


def test_ls_without_host_lists_every_bench_and_ignores_old_ones(disc, board):
    code, r = disc("ls")
    assert code == 0
    assert sorted((d["bench"], d["key"]) for d in r["devices"]) == [("bench-b", "otra-placa"),
                                                                    ("bench-sim", "sim-board")]
    code, r = disc("ls", "--bench", "bench-b")
    assert [d["key"] for d in r["devices"]] == ["otra-placa"]
    code, r = disc("ls", "--bench", "nada")
    assert code == 7 and r["error"] == "not_found" and "bench-sim" in r["message"]


def test_ls_with_host_is_one_bench_as_before(cli, benches_net, board):
    code, r = cli("ls")
    assert code == 0 and [d["key"] for d in r["devices"]] == ["sim-board"] and "bench" not in r["devices"][0]


def test_dev_commands_find_the_bench_of_the_board(disc, board):
    code, r = disc("status", "sim-board")
    assert code == 0 and r["board"] == "sim-board" and r["bench"] == "bench-sim" and r["state"] == "monitoring"
    code, r = disc("who", "sim-board@bench-sim")
    assert code == 0 and r["bench"] == "bench-sim" and r["lock_user"] is None
    code, r = disc("reserve", "sim-board", "--ttl", "1m")
    assert code == 0 and r["bench"] == "bench-sim"
    code, r = disc("send", "sim-board", "status", "--until", "OK", "--timeout", "5s")
    assert code == 0 and "OK" in r["match"]
    assert disc("release", "sim-board")[0] == 0


def test_dev_not_found_and_ambiguous(disc, benches_net, board):
    code, r = disc("status", "nada")
    assert code == 7 and r["error"] == "not_found"
    benches_net["b"].devices.append(dict(OTHER, device_key="sim-board", mac="AA:BB:CC:DD:EE:98"))
    code, r = disc("status", "sim-board")
    assert code == 7 and r["error"] == "ambiguous"
    assert sorted(m["bench"] for m in r["matches"]) == ["bench-b", "bench-sim"]
    code, r = disc("status", "sim-board@bench-sim")
    assert code == 0 and r["bench"] == "bench-sim"
    code, r = disc("status", "sim-board", "--bench", "bench-sim")
    assert code == 0 and r["bench"] == "bench-sim"


def test_discovery_cache_and_new_bench(disc, benches_net, board, tmp_path):
    """La lista de benches se cachea; una placa que no está en la cache fuerza un scan nuevo."""
    assert disc("ls")[0] == 0
    cache = tmp_path / "state-agent" / "benches.json"
    assert sorted(b["name"] for b in json.loads(cache.read_text())["benches"]) == ["127.0.0.1", "bench-b",
                                                                                     "bench-sim"]
    late = _FakeBench({"app": "espbench", "version": "0.34.0", "name": "bench-late"},
                      [dict(OTHER, device_key="nueva", mac="AA:BB:CC:DD:EE:77")])
    try:
        cfg = json.loads(pathlib.Path(os.environ["ESPBENCH_BENCHES_CONFIG"]).read_text())
        cfg["hosts"].append(late.host)
        pathlib.Path(os.environ["ESPBENCH_BENCHES_CONFIG"]).write_text(json.dumps(cfg))
        code, r = disc("ls")
        assert "bench-late" not in {d["bench"] for d in r["devices"]}       # de la cache
        code, r = disc("who", "nueva")                                        # no está: escanea de nuevo
        assert code == 0 and r["bench"] == "bench-late"
    finally:
        late.close()


def test_host_and_bench_together_is_bad_request(cli, benches_net):
    code, r = cli("ls", "--bench", "bench-sim", "--host", "127.0.0.1:1")
    assert code == 1 and r["error"] == "bad_request"


def test_bench_flag_overrides_env_host(cli, benches_net, board):
    code, r = cli("ls", "--bench", "bench-sim", host="127.0.0.1:1")
    assert code == 0 and [d["bench"] for d in r["devices"]] == ["bench-sim"]


def test_events_all_without_host(disc, board):
    disc("send", "sim-board", "hola", "--no-enter")
    code, r = disc("events", "--all", "--type", "send")
    assert code == 0 and r["events"] and all(e["bench"] == "bench-sim" for e in r["events"])


def test_human_ls_and_benches_without_host(disc, board):
    code, out, err = disc("ls", as_json=False)
    assert code == 0 and out.splitlines()[0].split()[:2] == ["BENCH", "UBICACIÓN"] and "sim-board" in out
    assert "Lab Chile" in out
    code, out, err = disc("benches", as_json=False)
    assert code == 0 and "viejo: ignorado" in out and "UBICACIÓN" in out and "Lab Chile" in out


def test_benches_and_ls_carry_the_bench_location(disc, board):
    code, r = disc("benches")
    by = {b["name"]: b for b in r["benches"]}
    assert (by["bench-b"]["location"], by["bench-sim"]["location"]) == ("Lab Chile", None)
    code, r = disc("ls")
    assert sorted((d["bench"], d["location"]) for d in r["devices"]) == [("bench-b", "Lab Chile"),
                                                                        ("bench-sim", None)]


def test_ls_filters_by_location(disc, board):
    from server import benchinfo
    benchinfo.set_location("Oficina BA")
    assert [d["key"] for d in disc("ls", "--location", "chile")[1]["devices"]] == ["otra-placa"]
    assert [d["key"] for d in disc("ls", "--location", "  oficina   ba ")[1]["devices"]] == ["sim-board"]
    assert disc("ls", "--location", "cordoba")[1]["devices"] == []
    assert [d["key"] for d in disc("ls", "--location", "")[1]["devices"]] == []        # todos tienen


def test_ls_with_host_filters_by_the_location_of_that_bench(cli, bench, board):
    from server import benchinfo
    assert [d["key"] for d in cli("ls", "--location", "")[1]["devices"]] == ["sim-board"]     # sin ubicación
    assert cli("ls", "--location", "lab")[1]["devices"] == []
    benchinfo.set_location("Lab Chile")
    assert [d["key"] for d in cli("ls", "--location", "LAB")[1]["devices"]] == ["sim-board"]
    assert cli("ls", "--location", "")[1]["devices"] == []
    assert "location" not in cli("ls")[1]["devices"][0]          # con host fijo, como antes


# ---------- nota y propiedades ----------

def test_note_set_and_clear(cli, board):
    code, r = cli("note", "sim-board", "testeando, no tocar")
    assert code == 0 and (r["note"], r["note_by"]) == ("testeando, no tocar", "agent") and r["note_at"]
    code, r = cli("ls")
    assert r["devices"][0]["note"] == "testeando, no tocar" and r["devices"][0]["available"]   # aviso, no lock
    code, r = cli("status", "sim-board")
    assert r["note"] == "testeando, no tocar" and r["note_by"] == "agent"
    code, r = cli("events", "sim-board", "--type", "note")
    assert r["events"][-1]["detail"] == {"text": "testeando, no tocar", "user": "agent"}
    code, r = cli("note", "sim-board", "--clear")
    assert code == 0 and r["note"] is None
    assert cli("note", "sim-board")[1]["error"] == "bad_request"
    assert cli("note", "sim-board", "x\ny")[1]["error"] == "bad_request"


def test_set_props_and_where(cli, board):
    code, r = cli("set", "sim-board", "chip=esp32-s3", "conectividad=wifi,lte", "uso+=agentes")
    assert code == 0 and r["props"] == {"chip": "esp32-s3", "conectividad": ["wifi", "lte"], "uso": ["agentes"]}
    assert r["changes"]["chip"] == {"from": None, "to": "esp32-s3"}
    code, r = cli("set", "sim-board", "conectividad-=wifi")
    assert r["props"]["conectividad"] == ["lte"]
    code, r = cli("set", "sim-board")
    assert r["props"]["chip"] == "esp32-s3"
    code, r = cli("set", "sim-board", "chip=esp32-s4")
    assert code == 1 and r["error"] == "bad_request" and "esp32-s3" in r["message"]
    assert [d["key"] for d in cli("ls", "--where", "chip=esp32-s3", "--where", "conectividad=lte")[1]["devices"]] \
        == ["sim-board"]
    assert cli("ls", "--where", "chip=esp32")[1]["devices"] == []
    assert cli("ls", "--where", "chipp=esp32")[1]["error"] == "bad_request"
    code, r = cli("events", "sim-board", "--type", "props")
    assert {"conectividad": {"from": ["wifi", "lte"], "to": ["lte"]}} in [e["detail"]["changes"] for e in r["events"]]
    assert all(e["detail"]["user"] == "agent" for e in r["events"])


def test_props_catalog_add_and_rm(cli, board):
    code, r = cli("props")
    assert code == 0 and [c["id"] for c in r["categories"]] == ["estado", "uso", "chip", "conectividad"]
    code, r = cli("props", "add", "estado", "prestada", "--desc", "prestada a otro equipo", "--exclude-pick")
    assert code == 0 and r["value"]["exclude_pick"] is True
    cli("set", "sim-board", "estado=prestada")
    code, r = cli("ls", "--free")
    assert r["devices"] == []                                   # el valor nuevo también excluye
    code, r = cli("props", "rm", "estado", "prestada")
    assert (code, r["error"]) == (1, "in_use") and "sim-board" in r["message"]
    cli("set", "sim-board", "estado=")
    assert cli("props", "rm", "estado", "prestada")[0] == 0


def test_estado_no_tocar_is_not_available_and_pick_skips_it(cli, bench, board):
    cli("set", "sim-board", "estado=no-tocar")
    code, r = cli("ls")
    assert r["devices"][0]["avoid"] is True and r["devices"][0]["available"] is False
    code, r = cli("pick")
    assert code == 7 and r["error"] == "not_found"
    cli("set", "sim-board", "estado=", "chip=esp32-c3")
    code, r = cli("pick", "--where", "chip=esp32-c3")
    assert code == 0 and r["board"] == "sim-board" and r["reserved"] is False


def test_pick_reserve_and_skip_taken(cli, bench, board):
    bench.add_board("ttyUSB1", "AA:BB:CC:DD:EE:02", key="sim-2")
    for k in ("sim-board", "sim-2"):
        cli("set", k, "uso+=agentes")
    assert cli("reserve", "sim-board", user="otro")[0] == 0         # la tiene otro: no está available
    code, r = cli("pick", "--where", "uso=agentes", "--reserve", "--ttl", "5m")
    assert code == 0 and r["board"] == "sim-2" and r["reserved"] and r["lock_user"] == "agent"
    code, r = cli("pick", "--where", "uso=agentes", "--reserve")
    assert code == 7 and r["error"] == "not_found"                 # la mía no: pick es para conseguir otra
    code, r = cli("pick", "--where", "uso=agentes", "--reserve", "--include-mine")
    assert code == 0 and r["board"] == "sim-2"
    assert cli("pick", "--where", "uso=agentes", "--reserve", user="tercero")[1]["error"] == "not_found"


def test_pick_reserve_tries_next_when_taken_in_between(cli, bench, board, monkeypatch):
    bench.add_board("ttyUSB1", "AA:BB:CC:DD:EE:02", key="sim-2")
    orig = lib.Client.reserve
    taken = []

    def racy(self, b, ttl):
        if not taken:                           # otro la toma justo antes
            taken.append(b.label)
            raise lib.EspbenchError("locked", "la tiene 'otro'")
        return orig(self, b, ttl)
    monkeypatch.setattr(lib.Client, "reserve", racy)
    code, r = cli("pick", "--reserve")
    assert code == 0 and r["reserved"] and r["board"] != taken[0]
    assert r["skipped"] == [{"board": taken[0], "bench": None, "error": "locked", "message": "la tiene 'otro'"}]


def test_pick_and_where_across_benches(disc, benches_net, board):
    benches_net["b"].devices[0]["props"] = {"chip": "esp32-s3"}
    code, r = disc("ls", "--where", "chip=esp32-s3")
    assert [(d["bench"], d["key"]) for d in r["devices"]] == [("bench-b", "otra-placa")]
    code, r = disc("pick", "--where", "chip=esp32-s3")
    assert code == 0 and (r["bench"], r["board"]) == ("bench-b", "otra-placa")
    code, r = disc("note", "sim-board", "dev alejo")
    assert code == 0 and r["bench"] == "bench-sim"


# ---------- revisión: discovery + nota/propiedades ----------

def test_props_with_several_benches(disc, benches_net, board):
    """add/rm necesitan --bench (el catálogo es de cada bench); sin acción, la unión con dónde está cada valor."""
    code, r = disc("props", "add", "chip", "esp32-p4")
    assert (code, r["error"]) == (1, "bad_request") and "--bench" in r["message"]
    code, r = disc("props", "add", "chip", "esp32-p4", "--bench", "bench-sim")
    assert code == 0 and r["bench"] == "bench-sim"
    code, r = disc("props")
    chip = next(c for c in r["categories"] if c["id"] == "chip")
    assert next(v for v in chip["values"] if v["id"] == "esp32-p4")["benches"] == ["bench-sim"]
    assert r["errors"][0]["bench"] == "bench-b" and r["errors"][0]["error"] == "unsupported"
    code, r = disc("set", "sim-board", "chip=esp32-p5")
    assert code == 1 and "--bench bench-sim" in r["message"]


def test_note_and_set_on_a_bench_without_properties_say_update_it(disc, benches_net, board):
    """bench-b es espbench (0.34.0) pero sin /api/properties: error claro, antes del PATCH."""
    for args in (("note", "otra-placa", "x"), ("set", "otra-placa", "chip=esp32")):
        code, r = disc(*args)
        assert (code, r["error"]) == (1, "unsupported"), r
        assert "bench-b" in r["message"] and "0.34.0" in r["message"] and "actualizalo" in r["message"]


def test_benches_reports_props_support_and_uses_the_bench_catalog(disc, benches_net, board):
    disc("props", "add", "estado", "prestada", "--exclude-pick", "--bench", "bench-sim")
    disc("set", "sim-board", "estado=prestada")
    by = {b["name"]: b for b in disc("benches")[1]["benches"]}
    assert by["bench-sim"]["props"] is True and by["bench-b"]["props"] is False
    assert by["bench-sim"]["available"] == 0          # estado con exclude_pick del catálogo, no el default


def test_pick_skips_my_own_boards_unless_include_mine(cli, bench, board):
    assert cli("reserve", "sim-board")[0] == 0
    code, r = cli("pick")
    assert (code, r["error"]) == (7, "not_found")
    code, r = cli("pick", "--include-mine")
    assert code == 0 and r["board"] == "sim-board"


def test_pick_reserve_skips_a_bench_that_rejects_the_token(cli, bench, board, monkeypatch):
    bench.add_board("ttyUSB1", "AA:BB:CC:DD:EE:02", key="sim-2")
    orig = lib.Client.reserve
    first = []

    def reserve(self, b, ttl):
        if not first:
            first.append(b.label)
            raise lib.EspbenchError("auth", "falta el token de la API")
        return orig(self, b, ttl)
    monkeypatch.setattr(lib.Client, "reserve", reserve)
    code, r = cli("pick", "--reserve")
    assert code == 0 and r["reserved"] and r["skipped"][0]["error"] == "auth"


def test_set_add_and_remove_in_the_same_category(cli, board):
    cli("set", "sim-board", "conectividad=wifi,lte")
    code, r = cli("set", "sim-board", "conectividad+=ble", "conectividad-=wifi")
    assert code == 0 and r["props"]["conectividad"] == ["lte", "ble"]
    assert cli("set", "sim-board", "uso+=ci", "uso+=demo")[1]["error"] == "bad_request"


def test_dev_at_bench_with_a_fixed_host(cli, benches_net, board):
    code, r = cli("status", "sim-board@bench-sim")
    assert code == 0 and r["board"] == "sim-board"
    code, r = cli("status", "sim-board@bench-b")
    assert (code, r["error"]) == (1, "bad_request") and "bench-sim" in r["message"]

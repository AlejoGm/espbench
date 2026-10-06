"""Tests de client/espbench_lib.py contra la Pi simulada (tests/benchsim.py):
server.api real (adaptador http.server, o uvicorn si está), placa con
DeviceManager/DeviceLog reales, tmux y esptool falsos."""
import json
import os
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "remote"))

from client import espbench_lib as lib  # noqa: E402
from server import locks, paths  # noqa: E402
from tests.benchsim import Bench  # noqa: E402

MAC = "AA:BB:CC:DD:EE:01"


@pytest.fixture(params=["adapter"])
def bench(request):
    if request.param == "uvicorn":
        pytest.importorskip("uvicorn")
    with Bench(request.param) as b:
        yield b


def make_client(bench, tmp_path, user="agent", token="t0k", state="state", **cfg) -> lib.Client:
    env = {"ESPBENCH_USER": user, "ESPBENCH_LOCK_TOKEN": token}
    config = lib.Config.load(host=cfg.pop("host", bench.host), env=env, cwd=tmp_path,
                             user_config=tmp_path / "no-config.json", **cfg)
    return lib.Client(config, poll_s=0.05, state_dir=tmp_path / state)


@pytest.fixture
def board(bench):
    return bench.add_board(key="sim-board")


@pytest.fixture
def client(bench, tmp_path):
    return make_client(bench, tmp_path)


def errcode(excinfo):
    return excinfo.value.error, excinfo.value.exit_code


# ---------- config ----------

def test_config_precedence(tmp_path):
    (tmp_path / ".flashcfg.json").write_text(json.dumps({"remote": [
        {"name": "otra", "host": "pi-otra", "lock_user": "x", "lock_token": "y"},
        {"name": "mi-board", "host": "pi-flashcfg", "token": "ft", "lock_user": "fu", "lock_token": "fl"}]}))
    ucfg = tmp_path / "espbench.json"
    ucfg.write_text(json.dumps({"default_profile": "lab", "profiles": {
        "lab": {"host": "pi-lab", "lock_user": "pu"}, "casa": {"host": "pi-casa"}}}))
    sub = tmp_path / "src" / "main"
    sub.mkdir(parents=True)
    c = lib.Config.load(env={}, cwd=sub, user_config=ucfg, device="mi-board")
    assert (c.host, c.lock_user, c.lock_token, c.token) == ("pi-lab", "pu", "fl", "ft")
    assert c.sources == {"host": "profile:lab", "lock_user": "profile:lab", "lock_token": ".flashcfg.json",
                         "token": ".flashcfg.json"}
    c = lib.Config.load(env={"ESPBENCH_HOST": "pi-env", "ESPBENCH_USER": "eu"}, cwd=sub, user_config=ucfg,
                        profile="casa")
    assert (c.host, c.lock_user, c.lock_token) == ("pi-env", "eu", "y")      # remote: el primero
    c = lib.Config.load(host="pi-flag:9000", env={"ESPBENCH_HOST": "pi-env"}, cwd=sub, user_config=ucfg)
    assert c.base_url == "http://pi-flag:9000" and c.hostname == "pi-flag"
    assert lib.Config.load(host="pi", env={}, cwd=tmp_path, user_config=ucfg).base_url == "http://pi:8080"
    with pytest.raises(lib.EspbenchError) as e:
        lib.Config.load(env={}, cwd=sub, user_config=ucfg, profile="no-existe")
    assert errcode(e) == ("bad_request", 1)


def test_missing_host_is_bad_request(tmp_path):
    c = lib.Config.load(env={}, cwd=tmp_path, user_config=tmp_path / "x.json")
    with pytest.raises(lib.EspbenchError) as e:
        c.base_url
    assert e.value.error == "bad_request"


def test_parse_duration():
    assert lib.parse_duration("300ms") == pytest.approx(0.3)
    assert [lib.parse_duration(x) for x in ("10s", "5m", "2h", "1.5", "1d")] == [10, 300, 7200, 1.5, 86400]
    with pytest.raises(lib.EspbenchError):
        lib.parse_duration("diez")


# ---------- resolve ----------

def test_resolve_by_key_sn_mac_or_tty(client, board):
    from common import mac_to_sn_sfy
    for name in ("sim-board", mac_to_sn_sfy(MAC), MAC, "aabbccddee01", "ttyUSB0"):
        b = client.resolve(name, write=True)
        assert (b.key, b.tty, b.mac, b.state) == ("AABBCCDDEE01", "ttyUSB0", MAC, "monitoring"), name


def test_resolve_unknown(client, board):
    with pytest.raises(lib.EspbenchError) as e:
        client.resolve("no-existe", write=True)
    assert errcode(e) == ("not_found", 7)
    b = client.resolve("no-existe")                    # lectura: la Pi decide (puede estar desconectada)
    with pytest.raises(lib.EspbenchError) as e:
        client.read_range(b.key)
    assert errcode(e) == ("not_found", 7)


def test_disconnected_board_is_readable_not_writable(client, board):
    board.device.disconnect()
    b = client.resolve("sim-board")
    assert b.state == "disconnected"
    r = client.read_range(b.key, until="boot")
    assert r["ok"] and r["until_found"] and r["session_ended"]       # el until apareció: vale
    with pytest.raises(lib.EspbenchError) as e:
        client.resolve("sim-board", write=True)
    assert errcode(e) == ("not_found", 7)


# ---------- esperas ----------

def test_send_until_idle_waits_for_output(client, board, monkeypatch):
    """Con idle_needs_output (send) el idle recién cuenta desde el primer byte
    nuevo: una placa que tarda en contestar no da un rango vacío."""
    real_keys, real_enter = board.keys, board.enter
    monkeypatch.setattr(board, "keys", lambda text: board.later(0.5, real_keys, text))
    monkeypatch.setattr(board, "enter", lambda: board.later(0.55, real_enter))
    b = client.resolve("sim-board", write=True)
    s = client.send(b, "status")
    r = client.read_range(b.key, since=s["cursor"], until="idle:200ms", echo="status", idle_needs_output=True,
                          timeout_s=5)
    assert r["ok"] and r["reason"] == "idle"
    assert any("OK uptime" in l for l in r["lines"])


def test_logs_until_idle_on_quiet_board(client, board):
    b = client.resolve("sim-board")
    t0 = time.monotonic()
    r = client.read_range(b.key, since="now", until="idle:300ms", timeout_s=5)
    assert r["ok"] and r["reason"] == "idle" and r["lines"] == [] and time.monotonic() - t0 < 2


def test_idle_never_reached_is_timeout(client, board):
    b = client.resolve("sim-board")
    stop = time.monotonic() + 1.5

    def chatter():
        if time.monotonic() < stop:
            board.serial("I (1) tick\r\n")
            board.later(0.05, chatter)
    chatter()
    r = client.read_range(b.key, since="now", until="idle:400ms", timeout_s=0.6)
    assert r["error"] == "timeout" and lib.exit_code(r["error"]) == 4


def test_for_window(client, board):
    b = client.resolve("sim-board")
    for i in range(5):
        board.later(0.2 + 0.1 * i, board.serial, f"I (1) tick {i}\r\n")
    t0 = time.monotonic()
    r = client.read_range(b.key, since="now", for_s=1.0)
    assert r["ok"] and r["reason"] == "for" and 1.0 <= time.monotonic() - t0 < 2.5
    assert [l.split(" ", 2)[2] for l in r["lines"]] == [f"I (1) tick {i}" for i in range(5)]


def test_until_pattern(client, board):
    b = client.resolve("sim-board", write=True)
    s = client.send(b, "status")
    r = client.read_range(b.key, since=s["cursor"], until="re:OK uptime=\\d+s", echo="status", timeout_s=5)
    assert r["ok"] and r["until_found"] and r["match"].endswith("> OK uptime=12s heap=210000")
    assert r["start"] == s["cursor"]


def test_echo_arriving_in_second_poll_does_not_match(client, board, monkeypatch):
    """El patrón también matchea el eco: el server lo saltea mientras el cliente
    mande `echo`, y el cliente lo manda hasta recibir echo_seen."""
    board.responses["version"] = ["version: simfw 1.0.0"]
    real_keys = board.keys
    monkeypatch.setattr(board, "keys", lambda text: board.later(0.3, real_keys, text))
    real_enter = board.enter
    monkeypatch.setattr(board, "enter", lambda: board.later(0.35, real_enter))
    polls = []
    real_log = client.board_log

    def spy(key, **params):
        r = real_log(key, **params)
        polls.append((params.get("echo"), r.get("echo_seen")))
        return r
    monkeypatch.setattr(client, "board_log", spy)
    b = client.resolve("sim-board", write=True)
    s = client.send(b, "version")
    r = client.read_range(b.key, since=s["cursor"], until="version", echo="version", timeout_s=5)
    assert r["ok"] and r["match"].endswith("version: simfw 1.0.0")
    assert polls[0] == ("version", None)                       # el eco no estaba en el primer poll
    seen = next(i for i, p in enumerate(polls) if p[1])
    assert all(p[0] == "version" for p in polls[:seen + 1]) and all(p[0] is None for p in polls[seen + 1:])


def test_logical_line_split_between_polls(client, board):
    b = client.resolve("sim-board")
    since = client.read_range(b.key, since="now")["end"]
    board.serial("result=")                     # sin \n: sale al archivo a los 150 ms
    board.later(0.5, board.serial, "OK\r\n")    # llega como ↪ en otro poll
    r = client.read_range(b.key, since=since, until="result=OK", timeout_s=5)
    assert r["ok"] and r["match"].endswith("> result=OK")
    assert any(l.endswith("↪ OK") for l in r["lines"])


def test_timeout(client, board):
    b = client.resolve("sim-board")
    r = client.read_range(b.key, since="now", until="nunca", timeout_s=0.4)
    assert r["error"] == "timeout" and not r["ok"] and r["until_found"] is False


def test_panic_while_waiting_is_crashed(client, board):
    b = client.resolve("sim-board", write=True)
    s = client.send(b, "panic")
    r = client.read_range(b.key, since=s["cursor"], until="OK", echo="panic", timeout_s=5)
    assert r["error"] == "crashed" and lib.exit_code(r["error"]) == 3
    assert r["crash"]["type"] == "panic" and r["crash"]["detail"]["reason"] == "LoadProhibited"
    assert any("Guru Meditation" in l for l in r["lines"])


def test_expect_panic_is_ok(client, board):
    b = client.resolve("sim-board", write=True)
    s = client.send(b, "panic")
    r = client.read_range(b.key, since=s["cursor"], until="OK", echo="panic", timeout_s=5, expect_panic=True)
    assert r["ok"] and r["reason"] == "panic" and r["crash"]["type"] == "panic"


def test_until_panic_is_not_a_crash(client, board):
    b = client.resolve("sim-board", write=True)
    s = client.send(b, "panic")
    r = client.read_range(b.key, since=s["cursor"], until="panic", timeout_s=5)
    assert r["ok"] and r["until_found"] and "Guru Meditation" in r["match"]


def test_crash_comes_from_events_not_from_log_responses(client, board, monkeypatch):
    """Un evento que se escribe después de que el poll respondió no aparece en
    los siguientes (empiezan en `end`): el crash sale de /events desde el start."""
    real_log = client.board_log
    monkeypatch.setattr(client, "board_log", lambda key, **p: {**real_log(key, **p), "events": []})
    b = client.resolve("sim-board", write=True)
    s = client.send(b, "panic")
    r = client.read_range(b.key, since=s["cursor"], for_s=1.0)
    assert r["error"] == "crashed" and r["crash"]["type"] == "panic"


def test_panic_continuing_the_prompt_is_detected(client, board):
    """El prompt `esp> ` (sin \\n) sale solo; el panic llega como `↪` y su evento
    queda con el cursor del prompt, antes del start del poll que lo trae. Antes
    se perdía y el reboot que sigue se tomaba como el crash."""
    b = client.resolve("sim-board")
    board.later(0.6, board.panic)          # el prompt del boot ya salió al archivo
    r = client.read_range(b.key, since="now", for_s=1.5, fail_on=("panic", "boot_loop", "boot"))
    assert r["error"] == "crashed" and r["crash"]["type"] == "panic", r.get("crash")


def test_session_ended_while_waiting(client, board):
    b = client.resolve("sim-board")
    board.later(0.3, board.device.disconnect)
    r = client.read_range(b.key, since="now", until="nunca", timeout_s=5)
    assert r["error"] == "session_ended" and lib.exit_code(r["error"]) == 9


def test_session_ended_after_until_found_is_ok(client, board):
    b = client.resolve("sim-board")
    since = client.read_range(b.key, since="now")["end"]
    board.serial("ready\r\n")
    time.sleep(0.1)
    board.device.disconnect()
    r = client.read_range(b.key, since=since, until="ready", timeout_s=5)
    assert r["ok"] and r["until_found"] and r["session_ended"]


def test_around_and_filters(client, board):
    b = client.resolve("sim-board", write=True)
    s = client.send(b, "panic")
    client.read_range(b.key, since=s["cursor"], until="panic", timeout_s=5)
    time.sleep(0.3)
    r = client.read_range(b.key, around="panic")
    assert r["ok"] and r["reason"] == "range" and r["lines"][0].endswith("rst:0x1 (POWERON_RESET),boot:0x13 "
                                                                        "(SPI_FAST_FLASH_BOOT)")
    assert any("Guru Meditation" in l for l in r["lines"])
    g = client.read_range(b.key, since="session", grep="Backtrace", src="serial", max_lines=5)
    assert len(g["lines"]) == 1 and "Backtrace" in g["lines"][0]


def test_bad_anchor_and_cursor_expired(client, board):
    b = client.resolve("sim-board")
    with pytest.raises(lib.EspbenchError) as e:
        client.read_range(b.key, since="ayer")
    assert errcode(e) == ("bad_anchor", 8)
    with pytest.raises(lib.EspbenchError) as e:
        client.read_range(b.key, since="c:20200101_000000_1:0", until="boot")
    assert errcode(e) == ("cursor_expired", 8)
    with pytest.raises(lib.EspbenchError) as e:
        client.read_range(b.key, grep="(")
    assert errcode(e) == ("bad_request", 1)


def test_client_side_truncation_across_polls(client, board):
    b = client.resolve("sim-board")
    for i in range(30):
        board.later(0.2 + 0.01 * i, board.serial, f"line {i}\r\n")
    r = client.read_range(b.key, since="now", for_s=0.8, max_lines=10)
    assert r["truncated"] and len(r["lines"]) == 11
    assert r["lines"][5] == "… 20 líneas omitidas …" and r["lines"][-1].endswith("line 29")


# ---------- escrituras: errores del contrato ----------

def test_busy_while_flashing(client, board):
    b = client.resolve("sim-board", write=True)
    board.device.start_flash()
    try:
        with pytest.raises(lib.EspbenchError) as e:
            client.send(b, "status")
        assert errcode(e) == ("busy", 5)
    finally:
        board.device.finish_flash()


def test_reserved_by_other_is_locked(bench, client, board, tmp_path):
    other = make_client(bench, tmp_path, user="juan", token="j", state="juan")
    other.reserve(other.resolve("sim-board", write=True), 600)
    b = client.resolve("sim-board", write=True)
    for call in (lambda: client.send(b, "status"), lambda: client.command(b, "reset"),
                 lambda: client.reserve(b, 60)):
        with pytest.raises(lib.EspbenchError) as e:
            call()
        assert errcode(e) == ("locked", 6)


def test_reservation_lost(bench, client, board, tmp_path):
    b = client.resolve("sim-board", write=True)
    client.reserve(b, 600)
    assert client.holds_reservation(b)
    assert client.send(b, "status")["ok"]                         # con require_reservation
    twin = make_client(bench, tmp_path, state="otra-maquina")       # mismo par, sin registro local
    twin.release(twin.resolve("sim-board", write=True))
    with pytest.raises(lib.EspbenchError) as e:
        client.send(b, "status")
    assert errcode(e) == ("reservation_lost", 6)
    assert "venció o la soltaron" in e.value.message and "espbench reserve sim-board" in e.value.message
    assert e.value.message.count("espbench reserve") == 1                 # sin la sugerencia repetida del server
    assert not client.holds_reservation(b)                           # la olvidó: la próxima no la exige
    assert client.send(b, "status")["ok"]


def test_expired_reservation_is_lost(client, board):
    b = client.resolve("sim-board", write=True)
    client.reserve(b, 600)
    locks.write("ttyUSB0", locks.Lock("agent", "t0k", int(time.time()) - 1, "AABBCCDDEE01"))
    with pytest.raises(lib.EspbenchError) as e:
        client.command(b, "reset")
    assert errcode(e) == ("reservation_lost", 6)


def test_token_mismatch(bench, client, board, tmp_path):
    b = client.resolve("sim-board", write=True)
    client.reserve(b, 600)
    wrong = make_client(bench, tmp_path, token="otro", state="wrong")
    with pytest.raises(lib.EspbenchError) as e:
        wrong.release(b)
    assert errcode(e) == ("token_mismatch", 6)


def test_device_changed(client, board):
    b = client.resolve("sim-board", write=True)
    b.mac = "11:22:33:44:55:66"                  # en el tty ahora hay otra placa
    with pytest.raises(lib.EspbenchError) as e:
        client.send(b, "status")
    assert errcode(e) == ("device_changed", 7)


def test_auth_and_auth_config(bench, client, board, tmp_path):
    paths.api_token_file().write_text("s3cret\n")
    b = client.resolve("sim-board", write=True)              # las lecturas siguen abiertas
    with pytest.raises(lib.EspbenchError) as e:
        client.send(b, "status")
    assert errcode(e) == ("auth", 10)
    good = lib.Client(lib.Config.load(host=bench.host, env={"ESPBENCH_TOKEN": "s3cret"}, cwd=tmp_path,
                                      user_config=tmp_path / "x.json"), poll_s=0.05, state_dir=tmp_path / "g")
    assert good.send(b, "status")["ok"]
    paths.api_token_file().unlink()
    paths.api_token_file().mkdir()                           # ilegible: falla cerrado
    with pytest.raises(lib.EspbenchError) as e:
        good.send(b, "status")
    assert errcode(e) == ("auth_config", 10)


def test_network_error(tmp_path):
    c = lib.Client(lib.Config.load(host="127.0.0.1:1", env={}, cwd=tmp_path, user_config=tmp_path / "x"),
                   http_timeout=2)
    with pytest.raises(lib.EspbenchError) as e:
        c.devices()
    assert errcode(e) == ("network", 10)


def test_write_body_never_forces(client, board):
    b = client.resolve("sim-board", write=True)
    client.reserve(b, 600)
    body = client._write_body(b, text="x")
    assert body == {"text": "x", "lock_user": "agent", "lock_token": "t0k", "expect_mac": MAC,
                    "require_reservation": True}


def test_reserve_needs_credentials(bench, board, tmp_path):
    c = make_client(bench, tmp_path, user="", token="")
    with pytest.raises(lib.EspbenchError) as e:
        c.reserve(c.resolve("sim-board", write=True), 60)
    assert errcode(e) == ("bad_request", 1)


# ---------- flash + verify ----------

def make_build(tmp_path, project="SIM-1_0") -> pathlib.Path:
    build = tmp_path / "build"
    build.mkdir()
    (build / "flasher_args.json").write_text(json.dumps(
        {"flash_files": {"0x1000": "bootloader/bootloader.bin", "0x8000": "partition_table/partition-table.bin",
                         "0x10000": "simfw.bin"}}))
    for rel in ("bootloader/bootloader.bin", "partition_table/partition-table.bin", "simfw.bin"):
        (build / rel).parent.mkdir(parents=True, exist_ok=True)
        (build / rel).write_bytes(b"\x00" * 64)
    (build / "simfw.elf").write_bytes(b"ELF")
    (build / "project_description.json").write_text(json.dumps({"project_name": project}))
    return build


def flash_and_verify(client, tmp_path, window=0.6, **kw):
    b = client.resolve("sim-board", write=True)
    r = client.flash(b, make_build(tmp_path), encrypt=False)
    assert r["ok"] and r["status"] == "exitoso" and r["cursor"].startswith("c:")
    return r, client.verify(b, r["cursor"], window_s=window, timeout_s=10, **kw)


def test_flash_verify_ok(client, board, tmp_path):
    r, v = flash_and_verify(client, tmp_path)
    assert v["ok"] and v["boot"].endswith("rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)")
    assert "new_session" not in v and v["reason"] == "for"
    assert board.flashes == 1 and board.monitor.calls == ["stop", "start"]


def test_flash_verify_follows_new_session(client, board, tmp_path):
    """S3/C3: el reset después del flash re-enumera el USB → sesión nueva. Verify
    sigue ahí en vez de salir con session_ended."""
    board.after_flash = board.replug_then_boot
    r, v = flash_and_verify(client, tmp_path)
    assert v["ok"], v
    assert v["new_session"] and v["new_session"] != lib.parse_cursor(r["cursor"])[0]
    assert v["boot_cursor"].startswith(f"c:{v['new_session']}:")
    assert "POWERON_RESET" in v["boot"]


def test_flash_verify_panic_in_window_is_crashed(client, board, tmp_path):
    board.after_flash = lambda: board.boot_then_panic(0.2)
    _, v = flash_and_verify(client, tmp_path, window=1.5)
    assert v["error"] == "crashed" and v["crash"]["type"] == "panic"


def test_flash_verify_expect_panic(client, board, tmp_path):
    board.after_flash = lambda: board.boot_then_panic(0.2)
    _, v = flash_and_verify(client, tmp_path, window=1.5, expect_panic=True)
    assert v["ok"] and v["reason"] == "panic"


def test_flash_verify_reboot_in_window_is_crashed(client, board, tmp_path):
    board.after_flash = lambda: (board.boot(), board.later(0.2, board.boot))
    _, v = flash_and_verify(client, tmp_path, window=1.0)
    assert v["error"] == "crashed" and v["crash"]["type"] == "boot"
    assert "reinició" in v["message"]


def test_flash_verify_until(client, board, tmp_path):
    board.after_flash = lambda: (board.boot(), board.later(0.8, board.serial, "I (900) wifi: connected\r\n"))
    _, v = flash_and_verify(client, tmp_path, window=0.3, until="wifi: connected")
    assert v["ok"] and v["match"].endswith("wifi: connected")
    assert any("wifi: connected" in l for l in v["lines"])


def test_flash_without_boot_is_timeout(client, board, tmp_path):
    board.after_flash = lambda: None
    b = client.resolve("sim-board", write=True)
    r = client.flash(b, make_build(tmp_path), encrypt=False)
    v = client.verify(b, r["cursor"], window_s=0.2, timeout_s=0.5)
    assert v["error"] == "timeout" and "boot" in v["message"]


def test_flash_failed(client, board, tmp_path):
    board.flash_rc = 2
    with pytest.raises(lib.EspbenchError) as e:
        client.flash(client.resolve("sim-board", write=True), make_build(tmp_path))
    assert errcode(e) == ("flash_failed", 2)
    assert e.value.data["error_hint"].startswith("Error fatal de conexión") and e.value.data["cursor"]


def test_flash_locked_by_other_reservation(bench, client, board, tmp_path):
    other = make_client(bench, tmp_path, user="juan", token="j", state="juan")
    other.reserve(other.resolve("sim-board", write=True), 600)
    with pytest.raises(lib.EspbenchError) as e:
        client.flash(client.resolve("sim-board", write=True), make_build(tmp_path))
    assert errcode(e) == ("locked", 6)


def test_flash_with_own_reservation_and_lost(bench, client, board, tmp_path):
    b = client.resolve("sim-board", write=True)
    client.reserve(b, 600)
    assert client.flash(client.resolve("sim-board", write=True), make_build(tmp_path), encrypt=False)["ok"]
    assert locks.read("ttyUSB0").reservation                # el flash no la vuelve permanente
    twin = make_client(bench, tmp_path, state="otra-maquina")
    twin.release(b)
    with pytest.raises(lib.EspbenchError) as e:
        client.flash(client.resolve("sim-board", write=True), tmp_path / "build")
    assert errcode(e) == ("reservation_lost", 6)


def test_flash_network_error(client, board, tmp_path):
    b = client.resolve("sim-board", write=True)
    b.port_tcp = 1
    with pytest.raises(lib.EspbenchError) as e:
        client.flash(b, make_build(tmp_path))
    assert errcode(e) == ("network", 10)


def test_flash_hw_model_mismatch_is_a_warning(client, board, tmp_path):
    from server.device_registry import DevicesFile
    DevicesFile().update_hw_model(MAC, "OTRO")
    r = client.flash(client.resolve("sim-board", write=True), make_build(tmp_path), encrypt=False)
    assert r["ok"] and r["warnings"] == ["hw_model distinto: artifact=SIM placa=OTRO"]


def test_reset_verify(client, board):
    b = client.resolve("sim-board", write=True)
    s = client.command(b, "reset")
    v = client.verify(b, s["cursor"], window_s=0.3, timeout_s=5)
    assert v["ok"] and "RTCWDT_RTC_RESET" in v["boot"]


def _boots_until_loop_start(client, board):
    """Boots "físicos" (botón EN: no pasan por el monitor) hasta que el próximo
    sea el inicio de un boot loop. add_board ya hizo uno."""
    from server.serial_watch import BOOT_LOOP_COUNT
    for _ in range(BOOT_LOOP_COUNT - 2):
        board.boot()
    board.wait_prompt()
    b = client.resolve("sim-board", write=True)
    return b, client.board_log(b.key, since="now", max_lines=1)["end"], client.crash_snapshot(b.key)


def test_verify_boot_loop_at_the_found_boot_is_informative(client, board):
    """C1: si el boot que encontró --verify es el que SerialWatch marcó como
    inicio de un boot loop (resets seguidos que no pasaron por el monitor), no
    es un crash del firmware: `boot_loop: true` informativo. Antes: exit 3."""
    b, since, known = _boots_until_loop_start(client, board)
    board.later(0.1, board.boot)
    v = client.verify(b, since, window_s=0.5, timeout_s=5, known=known)
    assert v["ok"] and v["boot_loop"] is True, v
    assert "RTCWDT" not in v["boot"] and "SW_CPU_RESET" in v["boot"]
    assert client.board_events(b.key, types="boot_loop", limit=5)["events"][-1]["detail"]["phase"] == "start"


def test_verify_reboot_in_the_window_during_a_boot_loop_is_crashed(client, board):
    """En un boot loop no hay eventos boot sueltos: la ventana ve el reinicio
    por su línea rst:. Si no, un firmware en loop pasaba el verify."""
    b, since, known = _boots_until_loop_start(client, board)
    board.later(0.1, board.boot)
    board.later(0.5, board.boot)
    v = client.verify(b, since, window_s=1.5, timeout_s=5, known=known)
    assert v["error"] == "crashed" and v["crash"]["type"] == "boot", v
    assert "reinició" in v["message"] and "rst:" in v["crash"]["detail"]["line"]


def test_restart_session(client, board):
    b = client.resolve("sim-board", write=True)
    old = client.board_events(b.key, limit=1)["session"]
    client.restart_session(b)
    deadline = time.monotonic() + 5
    while client.board_events(b.key, limit=1)["session"] == old and time.monotonic() < deadline:
        time.sleep(0.05)
    assert client.board_events(b.key, limit=1)["session"] != old
    assert client.wait_ready(b, timeout_s=5).state == "monitoring"


# ---------- con el server real (uvicorn), si está instalado ----------

@pytest.mark.parametrize("bench", ["uvicorn"], indirect=True)
def test_against_uvicorn(bench, client, board, tmp_path):
    b = client.resolve("sim-board", write=True)
    client.reserve(b, 600)
    s = client.send(b, "status")
    r = client.read_range(b.key, since=s["cursor"], until="OK", echo="status", timeout_s=5)
    assert r["ok"] and r["match"].endswith("OK uptime=12s heap=210000")
    s = client.send(b, "panic")
    assert client.read_range(b.key, since=s["cursor"], until="OK", timeout_s=5)["error"] == "crashed"
    time.sleep(0.3)
    assert any("Guru" in l for l in client.read_range(b.key, around="panic")["lines"])
    with pytest.raises(lib.EspbenchError) as e:
        client.read_range(b.key, since="ayer")
    assert errcode(e) == ("bad_anchor", 8)
    other = make_client(bench, tmp_path, user="juan", token="j", state="juan")
    with pytest.raises(lib.EspbenchError) as e:
        other.send(b, "status")
    assert errcode(e) == ("locked", 6)
    r, v = flash_and_verify(client, tmp_path, window=0.3)
    assert v["ok"]
    client.release(b)


def test_restart_session_respects_reservation_and_session_down(bench, client, board, tmp_path):
    other = make_client(bench, tmp_path, user="juan", token="j", state="juan")
    other.reserve(other.resolve("sim-board", write=True), 600)
    with pytest.raises(lib.EspbenchError) as e:
        client.restart_session(client.resolve("sim-board", write=True))
    assert errcode(e) == ("locked", 6)
    other.release(other.resolve("sim-board", write=True))
    del bench.boards["ttyUSB0"]                     # tmux ya no tiene la sesión
    try:
        with pytest.raises(lib.EspbenchError) as e:
            client.send(client.resolve("sim-board", write=True), "status")
        assert errcode(e) == ("session_down", 7)
    finally:
        bench.boards["ttyUSB0"] = board


# ---------- revisión de la fase 3 ----------

@pytest.mark.parametrize("delay", [0.05, 0.4])
def test_verify_late_boot_event_is_not_a_reboot_in_the_window(client, board, monkeypatch, delay):
    """El boot se encuentra por su línea; su evento llega después (Pi cargada).
    Antes, la ventana lo tomaba como "nuevo" y daba "se reinició en la ventana"."""
    import threading
    from server import device_log
    orig = device_log.DeviceLog.event
    slow = {"on": False}

    def late_boot(self, type_, detail=None, cursor=None, *a, **k):
        if type_ == "boot" and slow["on"]:
            threading.Timer(delay, orig, (self, type_, detail, cursor) + a, k).start()
            return None
        return orig(self, type_, detail, cursor, *a, **k)
    monkeypatch.setattr(device_log.DeviceLog, "event", late_boot)
    b = client.resolve("sim-board", write=True)
    known = client.crash_snapshot(b.key)
    slow["on"] = True
    s = client.command(b, "reset")
    v = client.verify(b, s["cursor"], window_s=1.0, timeout_s=5, known=known)
    assert v["ok"], v


def test_idle_does_not_stop_on_the_echo(client, board):
    """Eco inmediato y respuesta a 1 s: idle:500ms espera la respuesta."""
    board.reply_delay = 1.0
    b = client.resolve("sim-board", write=True)
    s = client.send(b, "status")
    r = client.read_range(b.key, since=s["cursor"], until="idle:500ms", echo="status", idle_needs_output=True,
                          timeout_s=5)
    assert r["ok"] and r["reason"] == "idle" and "until_found" not in r
    assert any(l.endswith("OK uptime=12s heap=210000") for l in r["lines"])


def test_idle_with_firmware_without_echo(client, board, monkeypatch):
    """Sin eco, después de D se cuenta desde la primera línea nueva: no espera
    un eco que no va a llegar."""
    monkeypatch.setattr(board, "keys", lambda text: setattr(board, "_typed", text))
    board.reply_delay = 0.1
    b = client.resolve("sim-board", write=True)
    s = client.send(b, "status")
    t0 = time.monotonic()
    r = client.read_range(b.key, since=s["cursor"], until="idle:400ms", echo="status", idle_needs_output=True,
                          timeout_s=5)
    assert r["ok"] and r["reason"] == "idle" and time.monotonic() - t0 < 2
    assert any(l.endswith("OK uptime=12s heap=210000") for l in r["lines"])


def test_snapshot_before_writing_catches_panic_on_the_prompt(client, board):
    """La foto de crashes se toma antes de escribir: un panic como `↪` del prompt
    que pasó entre la escritura y el inicio de la espera es nuevo igual."""
    b = client.resolve("sim-board")
    known = client.crash_snapshot(b.key)
    since = client.read_range(b.key, since="now")["end"]
    board.panic()
    time.sleep(0.3)
    late = client.read_range(b.key, since=since, for_s=0.2)            # foto tomada después: no lo ve
    assert late["ok"]
    r = client.read_range(b.key, since=since, for_s=0.2, known=known)
    assert r["error"] == "crashed" and r["crash"]["type"] == "panic"


def test_snapshot_failure_disables_the_new_rule(client, board, monkeypatch):
    b = client.resolve("sim-board")
    board.panic()                                      # panic viejo, en el prompt (cursor anterior)
    time.sleep(0.3)

    def broken(*a, **k):
        raise lib.EspbenchError("unexpected", "500")
    monkeypatch.setattr(client, "board_events", broken)
    assert client.crash_snapshot(b.key) is None
    monkeypatch.setattr(client, "board_events", lib.Client.board_events.__get__(client))
    r = client.read_range(b.key, since="now", for_s=0.2, known=None)
    assert r["ok"]


def test_events_dedupe_keeps_distinct_events_with_same_cursor():
    acc = lib._Range(10)
    a = {"type": "state", "cursor": "c:20261006_000000_1:10", "detail": {"from": "monitoring", "to": "flashing"}}
    b = {"type": "state", "cursor": "c:20261006_000000_1:10", "detail": {"from": "flashing", "to": "monitoring"}}
    acc.add({"start": "c:20261006_000000_1:0", "end": "c:20261006_000000_1:20", "lines": [], "events": [a]})
    acc.add({"start": "c:20261006_000000_1:0", "end": "c:20261006_000000_1:20", "lines": [], "events": [a, b]})
    assert acc.events == [a, b]


def test_compact_result(client, board):
    """Sin campos vacíos ni los eventos del api en el punto de partida."""
    b = client.resolve("sim-board", write=True)
    client.reserve(b, 600)
    s = client.send(b, "status")
    r = client.read_range(b.key, since=s["cursor"], until="OK", echo="status", timeout_s=5)
    assert set(r) == {"ok", "board", "reason", "date", "lines", "start", "end", "until_found", "match",
                      "match_cursor"}
    h = client.read_range(b.key, since="session")
    assert "until_found" not in h and "session_ended" not in h and "truncated" not in h


def test_reservation_registry_is_per_board_not_per_host_spelling(bench, board, tmp_path):
    """"127.0.0.1:P" y "http://127.0.0.1:P" son la misma Pi: antes eran dos claves
    y el segundo cliente no mandaba require_reservation."""
    a = make_client(bench, tmp_path, state="shared")
    b = make_client(bench, tmp_path, state="shared", host=f"http://{bench.host}")
    a.reserve(a.resolve("sim-board", write=True), 600)
    assert b.holds_reservation(b.resolve("sim-board", write=True))
    assert list(json.loads((tmp_path / "shared" / "reservations.json").read_text())) == ["AABBCCDDEE01"]


def test_reservation_registry_concurrent_updates(bench, tmp_path):
    import threading
    c = make_client(bench, tmp_path, state="conc")
    boards = [lib.Board(name=f"b{i}", key="x", mac=f"AA:BB:CC:00:00:{i:02X}") for i in range(20)]

    def add(bd):
        c._resv_update(lambda data: data.__setitem__(c._resv_key(bd), {"user": "agent"}))
    threads = [threading.Thread(target=add, args=(bd,)) for bd in boards]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(json.loads((tmp_path / "conc" / "reservations.json").read_text())) == 20


def test_around_cursor_from_events_after_session_change(client, board):
    """Lo que dice la skill: después de un cambio de sesión `--around panic` da
    bad_anchor (busca en la sesión actual); con el cursor de `events`, anda."""
    b = client.resolve("sim-board", write=True)
    s = client.send(b, "panic")
    client.read_range(b.key, since=s["cursor"], until="panic", timeout_s=5)
    time.sleep(0.2)
    board.replug()
    board.wait_prompt()
    with pytest.raises(lib.EspbenchError) as e:
        client.read_range(b.key, around="panic")
    assert errcode(e) == ("bad_anchor", 8)
    (panic,) = client.board_events(b.key, types="panic")["events"]
    r = client.read_range(b.key, around=panic["cursor"])
    assert any("Guru Meditation" in l for l in r["lines"]) and r["session_ended"]


def test_config_host_auto_means_discovery(tmp_path):
    (tmp_path / ".flashcfg.json").write_text(json.dumps({"remote": [{"name": "x", "host": "auto"}]}))
    cfg = lib.Config.load(env={}, cwd=tmp_path, user_config=tmp_path / "no.json", device="x")
    assert cfg.host is None and cfg.discovery
    assert not lib.Config.load(host="pi", env={}, cwd=tmp_path, user_config=tmp_path / "no.json").discovery


# ---------- nota y propiedades (sin red) ----------

def test_parse_set_ops():
    props, add, rm = lib.parse_set_ops(["chip=ESP32-S3", "estado=", "conectividad=wifi,lte", "uso+=ci", "perifericos-=modbus"])
    assert props == {"chip": "esp32-s3", "estado": None, "conectividad": ["wifi", "lte"]}
    assert (add, rm) == ({"uso": ["ci"]}, {"perifericos": ["modbus"]})
    for bad in (["chip"], ["chip==x"], ["uso+="], ["chip=a", "chip=b"], ["-chip=a"]):
        with pytest.raises(lib.EspbenchError) as e:
            lib.parse_set_ops(bad)
        assert e.value.error == "bad_request"


def test_where_and_pick_order():
    assert lib.parse_where(["chip=esp32-s3", "estado="]) == [("chip", "esp32-s3"), ("estado", None)]
    with pytest.raises(lib.EspbenchError):
        lib.parse_where(["chip"])
    base = {"mac": "AA", "state": "monitoring"}
    a = lib.summarize_device({**base, "device_key": "a", "props": {"chip": "esp32-s3", "conectividad": ["wifi", "lte"]}})
    b = lib.summarize_device({**base, "device_key": "b", "props": {"chip": "esp32-s3"}, "note": "dev alejo"})
    c = lib.summarize_device({**base, "device_key": "c", "props": {"chip": "esp32-s3", "estado": "no-tocar"}})
    d = lib.summarize_device({**base, "device_key": "d", "props": {"chip": "esp32-s3"}, "health": {"boot_loop": True}})
    e = lib.summarize_device({**base, "device_key": "e", "props": {"chip": "esp32-s3"}, "lock_user": "otro"})
    assert c["avoid"] and not c["available"] and a["available"] and not a["avoid"]
    assert lib.matches_where(a, [("conectividad", "lte"), ("chip", "esp32-s3")])
    assert not lib.matches_where(b, [("conectividad", "lte")])
    assert lib.matches_where(b, [("estado", None)]) and not lib.matches_where(c, [("estado", None)])
    assert [s["key"] for s in lib.pick_order([b, c, d, e, a], [("chip", "esp32-s3")])] == ["a", "b"]   # sin nota primero
    assert lib.summarize_device({**base, "props": {"estado": "prestada"}},
                                exclude={"estado": ("prestada",)})["avoid"]


def test_check_props_suggests_and_merge_categories():
    cats1 = [{"id": "chip", "multi": False, "values": [{"id": "esp32"}, {"id": "esp32-s3"}]}]
    cats2 = [{"id": "chip", "multi": False, "values": [{"id": "esp32"}, {"id": "esp32-p4"}]},
             {"id": "uso", "multi": True, "values": [{"id": "ci"}]}]
    merged = lib.merge_categories([cats1, cats2])
    assert [(c["id"], [v["id"] for v in c["values"]]) for c in merged] == [("chip", ["esp32", "esp32-s3", "esp32-p4"]),
                                                                           ("uso", ["ci"])]
    lib.check_props([("chip", "esp32-p4"), ("uso", None)], merged)
    with pytest.raises(lib.EspbenchError, match=r"¿'esp32-s3'\?"):
        lib.check_props([("chip", "esp32-s4")], merged)
    with pytest.raises(lib.EspbenchError, match=r"¿'chip'\?"):
        lib.check_props([("chips", "x")], merged)



def test_tokens_per_bench(tmp_path, monkeypatch):
    from client import benches as bmod
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"tailscale": False, "hosts": [], "tokens": {"lab": "t-lab"}}))
    monkeypatch.setenv("ESPBENCH_BENCHES_CONFIG", str(f))
    lab = bmod.Bench(name="lab", url="http://lab:8080", address="lab", port=8080, source="config")
    other = bmod.Bench(name="otro", url="http://otro:8080", address="otro", port=8080, source="config")
    cfg = lib.Config.load(env={"ESPBENCH_USER": "a"}, cwd=tmp_path, user_config=tmp_path / "no.json")
    assert lib.config_for_bench(cfg, lab).token == "t-lab" and lib.config_for_bench(cfg, other).token == ""
    cfg = lib.Config.load(env={"ESPBENCH_TOKEN": "global"}, cwd=tmp_path, user_config=tmp_path / "no.json")
    assert lib.config_for_bench(cfg, lab).token == "global"          # el env explícito gana


def test_pick_order_excludes_mine_by_default():
    base = {"mac": "AA", "state": "monitoring"}
    mine = lib.summarize_device({**base, "device_key": "m", "lock_user": "yo"}, "yo")
    free = lib.summarize_device({**base, "device_key": "f"}, "yo")
    assert [s["key"] for s in lib.pick_order([mine, free], me="yo")] == ["f"]
    assert [s["key"] for s in lib.pick_order([mine, free], me="yo", include_mine=True)] == ["m", "f"]

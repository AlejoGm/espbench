"""WiFi del banco (docs/specs/bench-wifi.md): benchwifi.py con el helper falso, /api/wifi, y el helper real
(remote/infra/espbench-wifi) con nmcli/iw/ip falsos. Un bench sin wifi.conf (una Pi) no cambia nada."""
import asyncio
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

from fastapi import HTTPException  # noqa: E402
from server import api, benchwifi, events, locks, paths, runstate  # noqa: E402

HELPER = ROOT / "remote/infra/espbench-wifi"
MAC = "AA:BB:CC:DD:EE:FF"
SID = "20261127_100000_7"
NOW = time.time()

STATUS_ON = """available=1
iface=wlx00c0ca1234
ssid=RedDelBanco
channel=6
present=1
active=1
--- stations
Station aa:bb:cc:dd:ee:ff (on wlx00c0ca1234)
	inactive time:	120 ms
	signal:  	-41 [-41] dBm
	connected time:	732 seconds
Station 11:22:33:44:55:66 (on wlx00c0ca1234)
	signal:  	-67 dBm
	connected time:	5 seconds
--- leases
1795700000 aa:bb:cc:dd:ee:ff 10.42.0.23 espressif 01:aa:bb:cc:dd:ee:ff
"""


class FakeHelper:
    """El helper: registra las llamadas; `up`/`down` cambian lo que contesta `status`."""

    def __init__(self):
        self.calls = []
        self.inputs = []
        self.active = False
        self.fail = None

    def __call__(self, args, input=None):
        self.calls.append(args[0])
        if input is not None:
            self.inputs.append(input)
        if self.fail == args[0]:
            return 1, "", "espbench-wifi: nmcli: Error: no se pudo"
        if args[0] == "configure":
            return 0, "OJO: 'RedDelBanco' ya está en el aire cerca del bench: las placas se pueden conectar a esa.\nok\n", ""
        if args[0] == "up":
            self.active = True
        elif args[0] == "down":
            self.active = False
        elif args[0] == "status":
            out = STATUS_ON if self.active else STATUS_ON.replace("active=1", "active=0").split("--- stations")[0] + \
                "--- stations\n--- leases\n"
            return 0, out, ""
        return 0, "", ""


@pytest.fixture
def wifi(monkeypatch):
    """Bench con WiFi configurado y el helper falso; sin timers reales."""
    paths.wifi_conf().parent.mkdir(parents=True, exist_ok=True)
    paths.wifi_conf().write_text("IFACE=wlx00c0ca1234\nSSID=RedDelBanco\nCHANNEL=6\n")
    fake = FakeHelper()
    monkeypatch.setattr(benchwifi, "runner", fake)
    monkeypatch.setattr(benchwifi, "_schedule", lambda st: None)
    return fake


def board(tty="ttyUSB0", mac=MAC):
    log = paths.device_output_log(mac)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(f"2026-11-27 10:00:00.000 | INFO  | devicelog      | sesión {SID} tty={tty}\n"
                    "2026-11-27 10:00:01.000 > I (5) WDrv: scan\n".encode())
    runstate.write(tty, {"mac": mac, "state": "monitoring", "log_path": str(log)})
    return log


def wifi_events(mac=MAC):
    return [e for e in events.read(paths.device_events_file(mac)) if e["type"] == "wifi"]


# ---------- sin WiFi (una Pi sin dongle) ----------

def test_bench_without_wifi_conf_changes_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(benchwifi, "runner", lambda args: called.append(args) or (0, "", ""))
    assert benchwifi.status() == {"available": False, "on": False}
    with pytest.raises(benchwifi.WifiError) as e:
        benchwifi.change("on")
    assert (e.value.status, e.value.error) == (409, "unsupported")
    benchwifi.resume()
    assert called == []                                   # el helper no se llama nunca
    assert api.wifi_status() == {"available": False, "on": False}
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.wifi_change(body={"action": "on"}))
    assert (e.value.status_code, e.value.detail["error"]) == (409, "unsupported")


# ---------- parse_status ----------

def test_parse_status_stations_and_leases():
    s = benchwifi.parse_status(STATUS_ON)
    assert s["available"] and s["active"] and s["present"] and s["channel"] == 6 and s["ssid"] == "RedDelBanco"
    assert s["stations"] == [{"mac": "AA:BB:CC:DD:EE:FF", "signal_dbm": -41, "connected_s": 732},
                             {"mac": "11:22:33:44:55:66", "signal_dbm": -67, "connected_s": 5}]
    assert s["leases"] == [{"mac": "AA:BB:CC:DD:EE:FF", "ip": "10.42.0.23", "hostname": "espressif"}]
    assert benchwifi.parse_status("available=0\n")["available"] is False


# ---------- on / off ----------

def test_on_off_status_and_events(wifi):
    board()
    st = benchwifi.status({"AABBCCDDEEFF": "mi-placa"})
    assert st["available"] and st["on"] is False and st["clients"] == [] and st["until"] is None
    r = benchwifi.change("on", 3600, user="facu", now=NOW, boards={"AABBCCDDEEFF": "mi-placa"})
    assert wifi.calls[-2:] == ["up", "status"] and r["on"] is True and r["by"] == "facu"
    assert r["until"] == benchwifi._iso(NOW + 3600)
    assert r["clients"][0] == {"mac": "AA:BB:CC:DD:EE:FF", "ip": "10.42.0.23", "signal_dbm": -41,
                               "connected_s": 732, "board": "mi-placa"}
    assert r["clients"][1]["board"] is None and r["clients"][1]["ip"] is None
    assert list(r["events"]) == ["AABBCCDDEEFF"] and r["events"]["AABBCCDDEEFF"].startswith(f"c:{SID}:")
    ev = wifi_events()
    assert len(ev) == 1 and ev[0]["detail"] == {"action": "on", "until": r["until"], "user": "facu"}
    assert "PASS" not in json.dumps(r) and "psk" not in json.dumps(r)
    r = benchwifi.change("off", user="facu", now=NOW + 10)
    assert r["on"] is False and r["until"] is None and wifi.calls[-2] == "down"
    assert [e["detail"]["action"] for e in wifi_events()] == ["on", "off"]


def test_on_duration_default_max_and_validation(wifi):
    r = benchwifi.change("on", now=NOW)
    assert r["until"] == benchwifi._iso(NOW + benchwifi.DEFAULT_ON_S)
    for bad in (0, -5, benchwifi.MAX_ON_S + 1, "diez"):
        with pytest.raises(benchwifi.WifiError) as e:
            benchwifi.change("on", bad, now=NOW)
        assert e.value.error == "bad_request"
    with pytest.raises(benchwifi.WifiError) as e:
        benchwifi.change("off", 60)
    assert e.value.error == "bad_request"
    with pytest.raises(benchwifi.WifiError) as e:
        benchwifi.change("prender")
    assert e.value.error == "bad_request"


def test_helper_failure_is_reported_and_state_unchanged(wifi):
    wifi.fail = "up"
    with pytest.raises(benchwifi.WifiError) as e:
        benchwifi.change("on", now=NOW)
    assert (e.value.status, e.value.error) == (502, "unexpected") and "no se pudo" in e.value.message
    assert benchwifi._load_state() == {}


# ---------- reservas ----------

def test_reserved_board_of_someone_else_blocks_unless_forced(wifi):
    board()
    locks.write("ttyUSB0", locks.Lock("juan", "x", expires=int(NOW) + 600))
    with pytest.raises(benchwifi.WifiError) as e:
        benchwifi.change("on", user="facu", lock_user="facu", lock_token="t", now=NOW)
    assert (e.value.status, e.value.error) == (423, "locked") and "juan" in e.value.message
    assert "up" not in wifi.calls
    # la reserva es mía: pasa
    r = benchwifi.change("on", user="juan", lock_user="juan", lock_token="x", now=NOW)
    assert r["on"]
    # forzado (dashboard): pasa y queda quién forzó en el evento
    r = benchwifi.change("off", user="facu", force=True, forced_by={"forced": True, "by_user": "facu"}, now=NOW)
    assert r["on"] is False and wifi_events()[-1]["detail"]["forced"] is True


def test_flash_lock_does_not_block():
    """El lock permanente de un flash no es una reserva: no bloquea el WiFi."""
    board()
    locks.write("ttyUSB0", locks.Lock("alejo", "t"))
    assert benchwifi.reserved_by_others("facu", "t") == []


# ---------- vencimiento y arranque ----------

def test_expire_due_turns_it_off_with_event(wifi):
    board()
    benchwifi.change("on", 60, user="facu", now=NOW)
    assert benchwifi.expire_due(NOW + 30) is False and wifi.active
    assert benchwifi.expire_due(NOW + 61) is True and not wifi.active
    assert wifi_events()[-1]["detail"] == {"action": "off", "reason": "vencida", "until": None, "user": None}
    assert benchwifi.expire_due(NOW + 120) is False                     # ya apagada


def test_resume_after_reboot_and_expired(wifi):
    benchwifi.change("on", 3600, user="facu", now=NOW)
    wifi.active = False                                                  # se reinició el bench: NM no la levanta
    benchwifi.resume(NOW + 60)
    assert benchwifi._load_state()["on"] is False and "up" not in wifi.calls[2:]
    benchwifi.change("on", 60, now=NOW)
    benchwifi.resume(NOW + 120)                                          # venció mientras el api no corría
    assert benchwifi._load_state()["on"] is False and wifi.calls[-1] == "down"


def test_status_when_dongle_unplugged_or_helper_fails(wifi):
    wifi.active = False
    orig = wifi.__call__
    wifi_unplugged = lambda args: (0, "available=1\niface=wlx1\nssid=S\nchannel=6\npresent=0\nactive=0\n"  # noqa: E731
                                     "--- stations\n--- leases\n", "") if args[0] == "status" else orig(args)
    benchwifi.runner = wifi_unplugged
    assert benchwifi.status()["error"] == "el dongle no está enchufado"
    benchwifi.runner = lambda args: (1, "", "sudo: a password is required")
    st = benchwifi.status()
    assert st["available"] and st["on"] is None and "password" in st["error"]


# ---------- API ----------

def test_api_wifi_change_and_status(wifi, monkeypatch):
    board()
    r = asyncio.run(api.wifi_change(body={"action": "on", "for_s": 120, "user": "agente", "lock_user": "agente",
                                          "lock_token": "t"}))
    assert r["on"] and r["by"] == "agente" and list(r["events"]) == ["AABBCCDDEEFF"]
    assert api.wifi_status()["on"] is True
    locks.write("ttyUSB0", locks.Lock("juan", "x", expires=int(time.time()) + 600))
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.wifi_change(body={"action": "off", "lock_user": "agente", "lock_token": "t"}))
    assert (e.value.status_code, e.value.detail["error"]) == (423, "locked")
    r = asyncio.run(api.wifi_change(body={"action": "off", "force": True, "lock_user": "dash", "lock_token": "t"}))
    assert r["on"] is False and wifi_events()[-1]["detail"]["by_user"] == "dash"
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.wifi_change(body={"action": "on", "for_s": "x"}))
    assert e.value.status_code == 400


def test_wifi_event_type_is_known():
    assert "wifi" in events.TYPES


# ---------- dongle sin configurar y configure() ----------

def fake_sys(root, ifaces, default=None):
    """sys/class/net/<if> (wireless + device → usb o mmc) y proc/net/route con la ruta default."""
    for name, bus in ifaces.items():
        dev = root / "devices" / bus / name
        dev.mkdir(parents=True, exist_ok=True)
        d = root / "sys/class/net" / name
        d.mkdir(parents=True, exist_ok=True)
        if bus != "pci":
            (d / "wireless").mkdir(exist_ok=True)
        (d / "device").symlink_to(dev)
    (root / "proc/net").mkdir(parents=True, exist_ok=True)
    rows = ["Iface\tDestination\tGateway"] + ([f"{default}\t00000000\t0105A8C0"] if default else [])
    (root / "proc/net/route").write_text("\n".join(rows) + "\n")
    return str(root)


def test_candidates_only_usb_wifi_that_is_not_the_uplink(tmp_path):
    root = fake_sys(tmp_path / "a", {"wlxb0487a": "pci0000:00/usb1/1-9", "wlan0": "platform/mmc1", "enp1s0": "pci"},
                    default="enp1s0")
    assert benchwifi.candidates(root) == ["wlxb0487a"]
    # una Pi: wlan0 interno (y su salida), sin dongle → nada
    pi = fake_sys(tmp_path / "pi", {"wlan0": "platform/mmc1", "eth0": "pci"}, default="wlan0")
    assert benchwifi.candidates(pi) == []
    # un dongle USB que es la salida del bench no se ofrece
    up = fake_sys(tmp_path / "up", {"wlxb0487a": "pci0000:00/usb1/1-9"}, default="wlxb0487a")
    assert benchwifi.candidates(up) == []
    assert benchwifi.candidates(str(tmp_path / "no-existe")) == []


@pytest.fixture
def dongle(monkeypatch):
    """Dongle enchufado, red sin configurar (sin wifi.conf), helper falso."""
    fake = FakeHelper()
    monkeypatch.setattr(benchwifi, "runner", fake)
    monkeypatch.setattr(benchwifi, "_schedule", lambda st: None)
    monkeypatch.setattr(benchwifi, "candidates", lambda root=None: ["wlxb0487a"])
    return fake


def test_unconfigured_dongle_status_and_on(dongle):
    st = benchwifi.status()
    assert st == {"available": True, "configured": False, "on": False, "iface": "wlxb0487a", "ssid": None,
                  "channel": None, "clients": [], "error": None}
    assert dongle.calls == []                               # sin wifi.conf no se llama al helper para el estado
    with pytest.raises(benchwifi.WifiError) as e:
        benchwifi.change("on")
    assert (e.value.status, e.value.error) == (409, "not_configured")


def test_configure_sends_secrets_by_stdin_and_never_returns_them(dongle):
    board()

    def configure_writes_conf(args, input=None):
        r = dongle(args, input)
        if args[0] == "configure":
            paths.wifi_conf().parent.mkdir(parents=True, exist_ok=True)
            paths.wifi_conf().write_text("IFACE=wlxb0487a\nSSID=RedDelBanco\nCHANNEL=11\n")
        return r
    benchwifi.runner = configure_writes_conf
    with pytest.raises(benchwifi.WifiError) as e:
        benchwifi.configure("RedDelBanco", "", 6)               # la primera vez la clave es obligatoria
    assert e.value.error == "bad_request" and "clave" in e.value.message
    r = benchwifi.configure("RedDelBanco", "una-clave-larga", "11", user="facu")
    assert dongle.inputs == ["RedDelBanco\nuna-clave-larga\n11\n"]
    assert r["configured"] and r["ssid"] == "RedDelBanco" and "ya está en el aire" in r["warning"]
    text = json.dumps(r) + json.dumps(wifi_events())
    assert "una-clave-larga" not in text
    assert wifi_events()[-1]["detail"] == {"action": "config", "ssid": "RedDelBanco", "channel": 11, "user": "facu"}
    # ya configurada: la clave vacía se manda vacía (el helper conserva la que estaba)
    benchwifi.configure("OtraRed", None, None)
    assert dongle.inputs[-1] == "OtraRed\n\n\n"


@pytest.mark.parametrize("ssid,password,channel,msg", [
    ("", "una-clave-larga", 6, "SSID"),
    ("x" * 33, "una-clave-larga", 6, "SSID"),
    ("con\nsalto", "una-clave-larga", 6, "SSID"),
    ("Red", "corta", 6, "clave"),
    ("Red", "contraseña-con-ñ", 6, "clave"),
    ("Red", "una-clave-larga", 14, "canal"),
    ("Red", "una-clave-larga", "x", "canal"),
])
def test_configure_validation(dongle, ssid, password, channel, msg):
    with pytest.raises(benchwifi.WifiError) as e:
        benchwifi.configure(ssid, password, channel)
    assert e.value.error == "bad_request" and msg in e.value.message
    assert "configure" not in dongle.calls


def test_configure_while_on_respects_reservations(wifi):
    board()
    benchwifi.change("on", now=NOW)
    locks.write("ttyUSB0", locks.Lock("juan", "x", expires=int(time.time()) + 600))
    with pytest.raises(benchwifi.WifiError) as e:
        benchwifi.configure("RedDelBanco", "una-clave-larga", 6, lock_user="facu", lock_token="t")
    assert e.value.error == "locked"
    r = benchwifi.configure("RedDelBanco", "una-clave-larga", 6, force=True, forced_by={"forced": True, "by_user": "facu"})
    assert wifi_events()[-1]["detail"]["forced"] is True and r["configured"]
    benchwifi.change("off", force=True)
    benchwifi.configure("RedDelBanco", None, 6, lock_user="facu", lock_token="t")      # apagada: no molesta a nadie


def test_configure_helper_error_is_bad_request(wifi):
    wifi.fail = "configure"
    with pytest.raises(benchwifi.WifiError) as e:
        benchwifi.configure("RedDelBanco", "una-clave-larga", 6)
    assert (e.value.status, e.value.error) == (400, "bad_request") and not e.value.message.startswith("espbench-wifi:")


def test_configure_without_any_wifi_is_unsupported(monkeypatch):
    monkeypatch.setattr(benchwifi, "candidates", lambda root=None: [])
    with pytest.raises(benchwifi.WifiError) as e:
        benchwifi.configure("RedDelBanco", "una-clave-larga", 6)
    assert e.value.error == "unsupported"


def test_api_wifi_config_never_echoes_the_password(dongle):
    paths.wifi_conf().parent.mkdir(parents=True, exist_ok=True)
    r = asyncio.run(api.wifi_config(body={"ssid": "RedDelBanco", "password": "una-clave-larga", "channel": 6,
                                          "user": "dash"}))
    assert "una-clave-larga" not in json.dumps(r)
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.wifi_config(body={"ssid": "", "password": "una-clave-larga"}))
    assert e.value.status_code == 400


# ---------- el helper real, con nmcli / iw / ip falsos ----------

FAKES = {
    "nmcli": r'''#!/bin/bash
echo "nmcli $*" >> "$FAKE/calls"
case "$*" in
  "-t -f NAME con show --active") [ -f "$FAKE/active" ] && echo espbench-wifi; exit 0 ;;
  "con up espbench-wifi") [ -f "$FAKE/fail_up" ] && { echo "Error: no AP" >&2; exit 4; }; touch "$FAKE/active" ;;
  "con down espbench-wifi") rm -f "$FAKE/active" ;;
  *"dev wifi list"*) cat "$FAKE/scan" 2>/dev/null ;;
esac
exit 0
''',
    "iw": r'''#!/bin/bash
echo "iw $*" >> "$FAKE/calls"
case "$*" in
  "dev") printf 'phy#0\n\tInterface wlan0\nphy#1\n\tInterface wlx00c0ca1234\n' ;;
  *"info") [ -f "$FAKE/present" ] || exit 1 ;;
  *"station dump") printf 'Station aa:bb:cc:dd:ee:ff (on wlx00c0ca1234)\n\tsignal:  \t-41 dBm\n\tconnected time:\t9 seconds\n' ;;
esac
exit 0
''',
    "ip": r'''#!/bin/bash
[ "$*" = "route show default" ] && echo "default via 192.168.1.1 dev wlan0 proto dhcp metric 600"
exit 0
''',
}


@pytest.fixture
def helper(tmp_path):
    fake, base, nm, leases = tmp_path / "fake", tmp_path / "esp", tmp_path / "nm", tmp_path / "leases"
    for d in (fake / "bin", base, nm, leases):
        d.mkdir(parents=True)
    for name, body in FAKES.items():
        (fake / "bin" / name).write_text(body)
        (fake / "bin" / name).chmod(0o755)
    sysnet = tmp_path / "sysnet"
    for name, bus in (("wlx00c0ca1234", "usb1/1-9"), ("wlan0", "mmc1")):
        dev = tmp_path / "devices" / bus / name
        dev.mkdir(parents=True)
        (sysnet / name).mkdir(parents=True)
        (sysnet / name / "device").symlink_to(dev)
    env = {"PATH": f"{fake / 'bin'}:/usr/bin:/bin", "ESP_BASE": str(base), "ESPBENCH_NM_DIR": str(nm),
           "ESPBENCH_NM_LEASES_DIR": str(leases), "ESPBENCH_WIFI_TEST": "1", "FAKE": str(fake),
           "ESPBENCH_SYS_NET": str(sysnet)}

    class H:
        def run(self, *args, stdin="", check=True):
            r = subprocess.run(["bash", str(HELPER), *args], env=env, input=stdin, capture_output=True, text=True)
            if check:
                assert r.returncode == 0, r.stderr
            return r

        def calls(self):
            p = fake / "calls"
            return p.read_text().splitlines() if p.exists() else []
    h = H()
    h.fake, h.base, h.nm, h.leases = fake, base, nm, leases
    return h


def test_helper_without_conf_is_unavailable_and_touches_nothing(helper):
    assert helper.run("status").stdout.startswith("available=0\n")
    for cmd in ("up", "down"):
        r = helper.run(cmd, check=False)
        assert r.returncode == 1 and "no está configurada" in r.stderr
    assert not any(c.startswith("nmcli") for c in helper.calls())
    assert helper.run("nada", check=False).returncode == 2


def test_helper_setup_writes_conf_and_keyfile_without_password_in_conf(helper):
    r = helper.run("setup", stdin="wlx00c0ca1234\nRedDelBanco\nclave-de-prueba\nclave-de-prueba\n\n")
    assert "Listo" in r.stdout
    conf = (helper.base / "wifi.conf").read_text()
    assert conf == "IFACE=wlx00c0ca1234\nSSID=RedDelBanco\nCHANNEL=6\n" and "clave" not in conf
    key = helper.nm / "espbench-wifi.nmconnection"
    text = key.read_text()
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    for want in ("autoconnect=false", "mode=ap", "ssid=RedDelBanco", "band=bg", "channel=6",
                 "psk=clave-de-prueba", "method=shared", "interface-name=wlx00c0ca1234"):
        assert want in text, want


def test_helper_setup_refuses_the_uplink_and_bad_input(helper):
    # wlan0 es por donde sale el bench (como en una Pi): no se toca
    r = helper.run("setup", stdin="wlan0\n", check=False)
    assert r.returncode == 1 and "incomunicado" in r.stderr
    assert not (helper.nm / "espbench-wifi.nmconnection").exists() and not (helper.base / "wifi.conf").exists()
    for stdin, msg in (("wlx00c0ca1234\nRedDelBanco\ncorta\ncorta\n\n", "entre 8 y 63"),
                       ("wlx00c0ca1234\nRedDelBanco\nclave-uno\nclave-dos\n\n", "no coinciden"),
                       ("wlx00c0ca1234\nRedDelBanco\nclave-de-prueba\nclave-de-prueba\n99\n", "canal"),
                       ("eth9\n", "no es una interfaz WiFi"),
                       ("wlx00c0ca1234\n\n", "falta el SSID")):                  # sin default: no va en el repo
        r = helper.run("setup", stdin=stdin, check=False)
        assert r.returncode == 1 and msg in r.stderr, (stdin, r.stderr)


def test_helper_setup_warns_if_ssid_already_on_air(helper):
    (helper.fake / "scan").write_text("OtraRed\nRedDelBanco\n")
    r = helper.run("setup", stdin="wlx00c0ca1234\nRedDelBanco\nclave-de-prueba\nclave-de-prueba\n\n")
    assert "ya está en el aire" in r.stdout


def test_helper_up_down_status(helper):
    helper.run("setup", stdin="wlx00c0ca1234\nRedDelBanco\nclave-de-prueba\nclave-de-prueba\n\n")
    (helper.fake / "present").touch()
    s = benchwifi.parse_status(helper.run("status").stdout)
    assert s["available"] and s["present"] and not s["active"] and s["stations"] == []
    helper.run("up")
    (helper.leases / "dnsmasq-wlx00c0ca1234.leases").write_text("1 aa:bb:cc:dd:ee:ff 10.42.0.9 * *\n")
    s = benchwifi.parse_status(helper.run("status").stdout)
    assert s["active"] and s["stations"][0]["mac"] == "AA:BB:CC:DD:EE:FF" and s["leases"][0]["ip"] == "10.42.0.9"
    helper.run("down")
    assert not benchwifi.parse_status(helper.run("status").stdout)["active"]
    helper.run("down")                                                   # apagada: no falla
    assert "nmcli con up espbench-wifi" in helper.calls()
    (helper.fake / "fail_up").touch()
    r = helper.run("up", check=False)
    assert r.returncode == 1 and "no AP" in r.stderr


def test_helper_needs_root(helper, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("corre como root")
    env = {"PATH": "/usr/bin:/bin", "ESP_BASE": str(helper.base)}
    (helper.base / "wifi.conf").write_text("IFACE=wlx1\nSSID=S\nCHANNEL=6\n")
    r = subprocess.run(["bash", str(HELPER), "up"], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "root" in r.stderr


def test_helper_configure_by_stdin_uses_the_dongle_and_keeps_the_password(helper):
    r = helper.run("configure", stdin="RedDelBanco\nuna-clave-larga\n\n")
    assert r.stdout.strip().endswith("ok")
    assert (helper.base / "wifi.conf").read_text() == "IFACE=wlx00c0ca1234\nSSID=RedDelBanco\nCHANNEL=6\n"
    key = helper.nm / "espbench-wifi.nmconnection"
    assert "psk=una-clave-larga" in key.read_text() and oct(key.stat().st_mode & 0o777) == "0o600"
    assert not any("una-clave-larga" in c for c in helper.calls())          # nunca como argumento
    # otra red, misma clave (vacía = la que estaba), canal nuevo; prendida → se vuelve a prender
    helper.run("up")
    helper.run("configure", stdin="OtraRed\n\n11\n")
    assert "psk=una-clave-larga" in key.read_text() and "ssid=OtraRed" in key.read_text()
    assert (helper.base / "wifi.conf").read_text().endswith("CHANNEL=11\n")
    assert "nmcli con up espbench-wifi" in helper.calls()[-3:] and (helper.fake / "active").exists()


def test_helper_configure_errors(helper):
    r = helper.run("configure", stdin="RedDelBanco\n\n\n", check=False)
    assert r.returncode == 1 and "falta la clave" in r.stderr
    r = helper.run("configure", stdin="RedDelBanco\ncorta\n\n", check=False)
    assert r.returncode == 1 and "entre 8 y 63" in r.stderr
    (helper.base / "nada").touch()
    for p in helper.base.parent.glob("sysnet/wlx*"):
        (p / "device").unlink()
        (p / "device").symlink_to(helper.base / "nada")                    # ya no es USB: no hay dongle
    r = helper.run("configure", stdin="RedDelBanco\nuna-clave-larga\n\n", check=False)
    assert r.returncode == 1 and "ningún dongle" in r.stderr


def test_helper_status_without_conf_reports_the_dongle(helper):
    assert helper.run("status").stdout == "available=0\ncandidate=wlx00c0ca1234\n"

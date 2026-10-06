"""client/benches.py: candidatos (config + Tailscale), sondeo, dedup y resolve.
Sin red: el HTTP es un get_json falso; una prueba usa un server HTTP real en localhost."""
import http.server
import json
import pathlib
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from client import benches
from client.benches import Bench, Candidate

TS_STATUS = {
    "Self": {"HostName": "AlexBook", "TailscaleIPs": ["100.1.1.1"], "Online": True},
    "Peer": {
        "k1": {"HostName": "sensipi02", "TailscaleIPs": ["100.75.179.122", "fd7a::1"], "Online": True},
        "k2": {"HostName": "sensipi03", "TailscaleIPs": ["100.124.234.106"], "Online": False},
        "k3": {"HostName": "Gustavo", "TailscaleIPs": ["100.91.94.66"], "Online": True},
        "k4": {"HostName": "v6only", "TailscaleIPs": ["fd7a::2"], "Online": True},
    },
}

DEV_A = {"tty_name": "esp-slot1", "tty": "/dev/esp-slot1", "port_tcp": 5001, "mac": "1C:C3:AB:01:61:D4",
         "sn": "SN0001", "device_key": "medidor-a"}
DEV_B = {"tty_name": "ttyUSB0", "tty": "/dev/ttyUSB0", "port_tcp": 5000, "mac": "AA:BB:CC:DD:EE:FF",
         "sn": "SN0002", "device_key": "medidor-b"}


def fake_net(routes):
    """get_json falso: {url: respuesta | excepción}. Lo que no está, connection refused."""
    def get_json(url, timeout):
        r = routes.get(url, ConnectionRefusedError("refused"))
        if isinstance(r, Exception):
            raise r
        return r
    return get_json


def test_tailscale_candidates_only_online_peers_prefer_ipv4():
    cands = benches.tailscale_candidates(TS_STATUS)
    assert [(c.address, c.label, c.source) for c in cands] == [
        ("100.75.179.122", "sensipi02", "tailscale"),
        ("100.91.94.66", "Gustavo", "tailscale"),
        ("fd7a::2", "v6only", "tailscale"),
    ]
    assert cands[2].url == "http://[fd7a::2]:8080"


def test_tailscale_candidates_without_status():
    assert benches.tailscale_candidates(None) == []
    assert benches.tailscale_candidates({"Peer": None}) == []


@pytest.mark.parametrize("entry,addr,port", [
    ("sensipi01", "sensipi01", 8080),
    ("10.0.0.5:9000", "10.0.0.5", 9000),
    ("[fd7a::9]:8081", "fd7a::9", 8081),
    ("[fd7a::9]", "fd7a::9", 8080),
])
def test_parse_host(entry, addr, port):
    c = benches.parse_host(entry)
    assert (c.address, c.port) == (addr, port)


def test_load_config_defaults_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv(benches.CONFIG_ENV, str(tmp_path / "nope.json"))
    assert benches.load_config() == {"tailscale": True, "hosts": [], "timeout_s": 2.0}
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"hosts": ["lab:8080"], "tailscale": False}))
    monkeypatch.setenv(benches.CONFIG_ENV, str(f))
    assert benches.load_config() == {"tailscale": False, "hosts": ["lab:8080"], "timeout_s": 2.0}


def test_candidates_config_first_tailscale_optional():
    cfg = {"hosts": ["sensipi01"], "tailscale": True}
    assert [c.label for c in benches.candidates(cfg, TS_STATUS)] == ["sensipi01", "sensipi02", "Gustavo", "v6only"]
    cfg["tailscale"] = False
    assert [c.label for c in benches.candidates(cfg, TS_STATUS)] == ["sensipi01"]


def test_probe_identifies_espbench_and_rejects_others():
    c = Candidate("100.75.179.122", label="sensipi02", source="tailscale")
    net = fake_net({c.url + "/api/version": {"app": "espbench", "version": "0.13.0", "name": "lab-cba"}})
    b = benches.probe(c, 1, net)
    assert (b.name, b.version, b.source, b.ok) == ("lab-cba", "0.13.0", "tailscale", True)

    other = fake_net({c.url + "/api/version": {"app": "grafana", "version": "10"}})
    assert benches.probe(c, 1, other) is None
    assert benches.probe(c, 1, fake_net({c.url + "/api/version": ["no", "dict"]})) is None
    assert benches.probe(c, 1, fake_net({})) is None


def test_probe_accepts_legacy_bench_named_by_source():
    c = Candidate("100.75.179.122", label="sensipi02", source="tailscale")
    b = benches.probe(c, 1, fake_net({c.url + "/api/version": {"version": "0.6.1"}}))
    assert b.name == "sensipi02" and b.version == "0.6.1"
    b = benches.probe(c, 1, fake_net({c.url + "/api/version": {"version": "0.31.8", "auth": False}}))
    assert b.name == "sensipi02" and b.version == "0.31.8"
    assert benches.probe(c, 1, fake_net({c.url + "/api/version": {"version": "1", "other": 1}})) is None


def test_scan_dedups_same_bench_seen_by_lan_and_tailscale():
    lan = "http://192.168.1.20:8080"
    ts = "http://100.75.179.122:8080"
    ident = {"app": "espbench", "version": "0.13.0", "name": "sensipi02"}
    net = fake_net({
        lan + "/api/version": ident, lan + "/api/devices": [DEV_A],
        ts + "/api/version": ident, ts + "/api/devices": [DEV_A],
        "http://100.91.94.66:8080/api/version": OSError("timeout"),
    })
    found = benches.scan({"hosts": ["192.168.1.20"], "tailscale": True}, TS_STATUS, net)
    assert [(b.name, b.url, b.source) for b in found] == [("sensipi02", lan, "config")]
    assert found[0].devices == [DEV_A]


def test_scan_marks_bench_whose_devices_fail():
    url = "http://lab:8080"
    net = fake_net({url + "/api/version": {"app": "espbench", "version": "1", "name": "lab"},
                    url + "/api/devices": ValueError("bad json")})
    [b] = benches.scan({"hosts": ["lab"], "tailscale": False}, None, net)
    assert not b.ok and "api/devices" in b.error and b.devices == []


def test_scan_empty():
    assert benches.scan({"hosts": [], "tailscale": False}) == []


def _bench(name, *devices):
    return Bench(name=name, url=f"http://{name}:8080", address=name, port=8080, source="config",
                 ok=True, devices=list(devices))


@pytest.mark.parametrize("key", ["medidor-a", "MEDIDOR-A", "sn0001", "1C:C3:AB:01:61:D4", "1cc3ab0161d4",
                                 "1C-C3-AB-01-61-D4", "pi2/esp-slot1", "pi2//dev/esp-slot1"])
def test_resolve_by_any_key(key):
    bs = [_bench("pi1", DEV_B), _bench("pi2", DEV_A)]
    bench, dev = benches.resolve(key, bs)
    assert bench.name == "pi2" and dev is DEV_A


def test_resolve_not_found_lists_benches():
    with pytest.raises(benches.ResolveError, match="pi1, pi2"):
        benches.resolve("nada", [_bench("pi1", DEV_B), _bench("pi2", DEV_A)])
    with pytest.raises(benches.ResolveError, match="ninguno"):
        benches.resolve("x", [])
    with pytest.raises(benches.ResolveError):
        benches.resolve("pi3/esp-slot1", [_bench("pi2", DEV_A)])


def test_resolve_ambiguous_across_benches():
    with pytest.raises(benches.ResolveError, match="pi1/esp-slot1, pi2/esp-slot1"):
        benches.resolve("medidor-a", [_bench("pi1", DEV_A), _bench("pi2", DEV_A)])


def test_device_without_key_or_mac_never_matches_empty():
    assert not benches.device_matches("", "pi", {"tty_name": "ttyUSB0"})
    assert not benches.device_matches("x", "pi", {"tty_name": "ttyUSB0", "mac": None, "sn": None})


def test_scan_against_real_http_server():
    """El get_json real (urllib) contra un server HTTP de verdad en localhost."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = {"/api/version": {"app": "espbench", "version": "0.13.0", "name": "local"},
                    "/api/devices": [DEV_B]}.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        found = benches.scan({"hosts": [f"127.0.0.1:{port}", "127.0.0.1:1"], "tailscale": False, "timeout_s": 2})
        assert [(b.name, b.devices) for b in found] == [("local", [DEV_B])]
    finally:
        srv.shutdown()


# ---------- deploy.py: host "auto" ----------

def test_deploy_host_auto_resolves_bench(monkeypatch):
    from client import deploy
    monkeypatch.setattr(deploy, "_benches_cache", None)
    calls = []

    def fake_scan():
        calls.append(1)
        return [_bench("pi1", DEV_B), _bench("pi2", DEV_A)]
    monkeypatch.setattr(benches, "scan", fake_scan)

    r = {"name": "medidor-a"}
    port, info = deploy._resolve_device_port(r)
    assert (port, r["host"], info) == (5001, "pi2", DEV_A)

    r2 = {"name": "medidor-b", "host": "auto", "port": 1234}   # con auto, el port se ignora
    assert deploy._resolve_device_port(r2)[0] == 5000 and r2["host"] == "pi1"
    assert len(calls) == 1   # un solo scan por corrida


def test_deploy_explicit_host_and_port_untouched():
    from client import deploy
    assert deploy._resolve_device_port({"host": "sensipi01", "port": 5003}) == (5003, None)


def test_probe_reads_host_id_and_scan_dedups_by_it():
    lan, ts = "http://192.168.1.20:8080", "http://100.75.179.122:8080"
    net = fake_net({
        lan + "/api/version": {"app": "espbench", "version": "1", "name": "lab", "id": "DC:A6:32:00:00:01"},
        lan + "/api/devices": [DEV_A],
        ts + "/api/version": {"app": "espbench", "version": "1", "name": "lab", "id": "dc:a6:32:00:00:01"},
        ts + "/api/devices": [DEV_A],
    })
    found = benches.scan({"hosts": ["192.168.1.20"], "tailscale": True}, TS_STATUS, net)
    assert [(b.name, b.id, b.key) for b in found] == [("lab", "dc:a6:32:00:00:01", "dc:a6:32:00:00:01")]


def test_scan_keeps_two_hosts_with_the_same_name():
    a, b = "http://10.0.0.1:8080", "http://10.0.0.2:8080"
    net = fake_net({a + "/api/version": {"app": "espbench", "version": "1", "name": "raspberrypi", "id": "aa:00:00:00:00:01"},
                    a + "/api/devices": [], b + "/api/devices": [],
                    b + "/api/version": {"app": "espbench", "version": "1", "name": "raspberrypi", "id": "aa:00:00:00:00:02"}})
    found = benches.scan({"hosts": ["10.0.0.1", "10.0.0.2"], "tailscale": False}, None, net)
    assert sorted(x.id for x in found) == ["aa:00:00:00:00:01", "aa:00:00:00:00:02"]


def test_legacy_bench_key_is_its_name():
    assert _bench("pi1").key == "name:pi1"

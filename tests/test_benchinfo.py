"""benchinfo: salud de la máquina (con /proc y /sys falsos) y actividad por hora desde events.jsonl."""
import asyncio
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server import api, benchinfo, events, paths, runstate
from server.device_registry import DeviceRegistry

MAC = "AA:BB:CC:DD:EE:FF"
NOW = time.mktime((2026, 10, 6, 12, 0, 0, 0, 0, -1))


def fake_root(tmp_path):
    r = tmp_path / "root"
    files = {
        "sys/class/thermal/thermal_zone0/temp": "68421\n",
        "proc/meminfo": "MemTotal:        3884324 kB\nMemFree:  100 kB\nMemAvailable:    2913243 kB\n",
        "proc/uptime": "533040.12 2000000.00\n",
        "proc/loadavg": "0.42 0.30 0.25 1/300 1234\n",
        "proc/device-tree/model": "Raspberry Pi 4 Model B Rev 1.4\x00",
    }
    for rel, text in files.items():
        f = r / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return r


def test_health_from_proc_and_sys(tmp_path):
    h = benchinfo.health(str(fake_root(tmp_path)), disk_path=str(tmp_path))
    assert h["temp_c"] == 68.4 and h["model"] == "Raspberry Pi 4 Model B Rev 1.4"
    assert h["ram"] == {"total_mb": 3793, "used_pct": 25}
    assert h["load"]["1m"] == 0.42 and h["load"]["cpus"] >= 1
    assert h["uptime_s"] == 533040
    assert 0 <= h["disk"]["used_pct"] <= 100 and h["disk"]["total_gb"] > 0
    assert h["hostname"]


def test_health_without_proc_is_all_none(tmp_path):
    h = benchinfo.health(str(tmp_path / "nada"), disk_path=str(tmp_path / "no-existe"))
    assert h["temp_c"] is None and h["ram"] is None and h["load"] is None and h["uptime_s"] is None
    assert h["disk"] is None and h["model"] is None


def ev(type_, hours_ago, **detail):
    return events.make(type_, "c:20261006_000000_1:0", detail, ts=NOW - hours_ago * 3600)


def write_events(evs, mac=MAC):
    for e in evs:
        events.append(paths.device_events_file(mac), e)


def test_activity_buckets_by_hour():
    write_events([
        ev("boot", 30),                       # fuera de la ventana
        ev("boot", 23.5), ev("flash", 23.4), ev("boot", 23.3),
        ev("reserve", 10),
        ev("boot_loop", 5, phase="start"), ev("boot_loop", 4.9, phase="end"),
        ev("panic", 0.5, kind="guru"), ev("boot", 0.49),
        ev("send", 0.2),                      # no se cuenta
    ])
    a = benchinfo.device_activity(paths.device_events_file(MAC), 24, NOW)
    b = a["buckets"]
    assert len(b) == 24
    assert b[0] == {"boot": 2, "panic": 0, "flash": 1, "boot_loop": 0, "reserve": 0}
    assert b[14]["reserve"] == 1
    assert b[19]["boot_loop"] == 1 and sum(x["boot_loop"] for x in b) == 1    # solo el inicio
    assert b[23]["panic"] == 1 and b[23]["boot"] == 1
    assert sum(x["boot"] for x in b) == 3
    assert [e["type"] for e in a["recent"]] == ["panic", "boot_loop", "flash"]     # más nuevo primero
    assert a["recent"][0]["detail"] == {"kind": "guru"}


def test_activity_without_events_file():
    a = benchinfo.device_activity(paths.device_events_file(MAC), 24, NOW)
    assert a["recent"] == [] and all(sum(x.values()) == 0 for x in a["buckets"])


def test_activity_per_device_skips_unknown_mac():
    write_events([ev("panic", 1)])
    out = benchinfo.activity([{"tty_name": "ttyUSB0", "mac": MAC, "device_key": "medidor"},
                              {"tty_name": "ttyUSB1", "mac": None}], hours=24, now=NOW)
    assert list(out["devices"]) == ["ttyUSB0"]
    assert out["devices"]["ttyUSB0"]["key"] == "medidor" and out["hours"] == 24
    assert out["devices"]["ttyUSB0"]["buckets"][23]["panic"] == 1


def test_api_endpoints(tmp_path, monkeypatch):
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "ttyUSB0").touch()
    monkeypatch.setattr(api, "registry", DeviceRegistry(dev_dir=str(dev)))
    log = paths.device_output_log(MAC)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("hola\n")
    os.utime(log, (NOW, NOW))
    runstate.write("ttyUSB0", {"mac": MAC, "state": "monitoring", "log_path": str(log), "pid": os.getpid()})
    write_events([events.make("panic", None, {"kind": "guru"})])

    act = asyncio.run(api.bench_activity(hours=500))
    assert act["hours"] == 168 and act["devices"]["ttyUSB0"]["buckets"][-1]["panic"] == 1
    devices = asyncio.run(api.get_devices())
    assert devices[0]["last_log_epoch"] == NOW
    h = asyncio.run(api.bench_health())
    assert set(h) >= {"temp_c", "ram", "disk", "load", "uptime_s", "hostname"}


def test_last_log_epoch_none_without_log(tmp_path):
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "ttyUSB0").touch()
    runstate.write("ttyUSB0", {"mac": MAC, "state": "monitoring", "log_path": str(tmp_path / "no.log")})
    assert DeviceRegistry(dev_dir=str(dev)).list_devices()[0].last_log_epoch is None

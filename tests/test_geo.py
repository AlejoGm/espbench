"""geo.py: ubicación del bench por IP pública (con HTTP falso: nada sale a la red), override
manual y desactivada."""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server import geo, paths

IPINFO = {"ip": "200.1.2.3", "city": "Santiago", "region": "Santiago Metropolitan", "country": "CL",
          "loc": "-33.4569,-70.6483", "timezone": "America/Santiago"}
IPAPI = {"ip": "200.1.2.3", "city": "Buenos Aires", "region": "Buenos Aires F.D.", "country": "AR",
         "country_code": "AR", "latitude": -34.6037, "longitude": -58.3816, "timezone": "America/Argentina/Buenos_Aires"}


@pytest.fixture(autouse=True)
def geo_on(monkeypatch):
    monkeypatch.delenv("ESPBENCH_GEO")        # conftest la apaga para todos los demás tests
    paths.meta_dir().mkdir(parents=True)


def net(answers):
    """get_json falso: url → dict, o una excepción para levantar."""
    calls = []

    def get(url, timeout):
        calls.append((url, timeout))
        a = answers.get(url, OSError("sin red"))
        if isinstance(a, BaseException):
            raise a
        return a
    get.calls = calls
    return get


def now():
    return "2026-10-06T12:00:00-03:00"


def test_ipinfo_first_with_short_timeout():
    get = net({geo.SERVICES[0]: IPINFO})
    d = geo.refresh(get, now)
    assert get.calls == [("https://ipinfo.io/json", 3.0)]
    assert {k: d[k] for k in ("city", "region", "country", "lat", "lon", "tz", "source", "ts", "stale")} == \
        {"city": "Santiago", "region": "Santiago Metropolitan", "country": "CL", "lat": -33.4569, "lon": -70.6483,
         "tz": "America/Santiago", "source": "auto", "ts": now(), "stale": False}
    loc = geo.location()
    assert loc["label"] == "Santiago, CL" and loc["source"] == "auto" and loc["lat"] == -33.4569


@pytest.mark.parametrize("first", [OSError("timeout"), ValueError("json roto"), {"error": {"title": "Rate limit"}},
                                   {"ip": "10.0.0.1", "bogon": True}])
def test_falls_back_to_ipapi(first):
    get = net({geo.SERVICES[0]: first, geo.SERVICES[1]: IPAPI})
    d = geo.refresh(get, now)
    assert [u for u, _ in get.calls] == list(geo.SERVICES)
    assert (d["city"], d["country"], d["lat"], d["lon"], d["service"]) == ("Buenos Aires", "AR", -34.6037, -58.3816,
                                                                            "ipapi.co")
    assert geo.location()["label"] == "Buenos Aires, AR"


def test_network_failure_keeps_the_last_one_marked_stale():
    geo.refresh(net({geo.SERVICES[0]: IPINFO}), now)
    get = net({})
    d = geo.refresh(get, lambda: "2026-10-07T12:00:00-03:00")
    assert len(get.calls) == 2                                     # un intento por servicio, sin reintentos
    assert d["stale"] is True and d["ts"] == now() and d["city"] == "Santiago"
    loc = geo.location()
    assert loc["stale"] is True and loc["label"] == "Santiago, CL"
    assert geo.refresh(net({geo.SERVICES[0]: IPINFO}), now)["stale"] is False


def test_failure_without_anything_saved_is_no_location():
    assert geo.refresh(net({}), now) is None
    assert geo.location() is None and not paths.bench_geo_file().exists()


def test_label_without_city():
    assert geo.label({"region": "Maule", "country": "CL"}) == "Maule, CL"
    assert geo.label({"country": "CL"}) == "CL"
    assert geo.label({}) is None


def test_manual_override_wins_and_clearing_it_goes_back_to_auto():
    geo.refresh(net({geo.SERVICES[0]: IPINFO}), now)
    geo.set_manual("Oficina BA (sale por VPN)")
    loc = geo.location()
    assert (loc["label"], loc["source"], loc["city"]) == ("Oficina BA (sale por VPN)", "manual", None)
    geo.set_manual("")
    assert geo.location()["source"] == "auto"


def test_disabled_does_not_ask_anyone_and_only_the_manual_counts(monkeypatch):
    geo.refresh(net({geo.SERVICES[0]: IPINFO}), now)
    paths.geo_disabled_file().touch()
    get = net({geo.SERVICES[0]: IPINFO})
    assert geo.refresh(get, now) is None and get.calls == []
    assert geo.location() is None                          # lo automático guardado no se muestra
    geo.set_manual("Lab")
    assert geo.location()["label"] == "Lab"
    paths.geo_disabled_file().unlink()
    monkeypatch.setenv("ESPBENCH_GEO", "off")
    assert geo.disabled()


def test_files_go_in_meta_with_esp_base_read_only():
    base = paths.esp_base()
    base.chmod(0o555)
    try:
        geo.refresh(net({geo.SERVICES[0]: IPINFO}), now)
        geo.set_manual("Lab")
        geo.set_manual(None)
    finally:
        base.chmod(0o755)
    assert json.loads(paths.bench_geo_file().read_text())["city"] == "Santiago"
    assert paths.bench_geo_file().parent == paths.meta_dir()


def test_background_asks_at_start_and_then_waits(monkeypatch):
    monkeypatch.setattr(geo, "_started", False)
    waits = []

    class Stop(Exception):
        pass

    def sleep(s):
        waits.append(s)
        if len(waits) >= 2:
            raise Stop

    started = []
    monkeypatch.setattr(geo.threading, "Thread", lambda target, **kw: type("T", (), {
        "start": lambda self: started.append(target)})())
    get = net({geo.SERVICES[0]: IPINFO})
    assert geo.start_background(get, sleep) is True
    assert geo.start_background(get, sleep) is False                     # una vez por proceso
    with pytest.raises(Stop):
        started[0]()
    assert len(get.calls) == 1                     # al arrancar consultó; la segunda vuelta, guardado fresco
    assert waits[0] == geo.REFRESH_S and geo.REFRESH_S - 5 < waits[1] <= geo.REFRESH_S


def test_background_after_a_failure_retries_in_an_hour(monkeypatch):
    monkeypatch.setattr(geo, "_started", False)
    waits = []

    def sleep(s):
        waits.append(s)
        raise KeyboardInterrupt

    started = []
    monkeypatch.setattr(geo.threading, "Thread", lambda target, **kw: type("T", (), {
        "start": lambda self: started.append(target)})())
    geo.start_background(net({}), sleep)
    with pytest.raises(KeyboardInterrupt):
        started[0]()
    assert waits == [geo.RETRY_S]


def test_background_disabled_does_not_start(monkeypatch):
    monkeypatch.setattr(geo, "_started", False)
    monkeypatch.setenv("ESPBENCH_GEO", "off")
    assert geo.start_background(net({})) is False

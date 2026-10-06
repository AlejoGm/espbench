"""bench-master (master/app.py): cache de benches, API propia y proxy HTTP/WS.

Necesita httpx y websockets (master/requirements.txt); sin ellos se saltea.
Correr con el venv del master:  master/.venv/bin/python -m pytest tests/test_master.py
"""
import asyncio
import pathlib
import socket
import sys
import threading
import time

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("websockets")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

import uvicorn  # noqa: E402
from fastapi import Body, FastAPI, Request, WebSocket  # noqa: E402
from fastapi.responses import PlainTextResponse, RedirectResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from client.benches import Bench  # noqa: E402
from master.app import BenchCache, create_app  # noqa: E402

DEV_A = {"tty_name": "esp-slot1", "port_tcp": 5001, "mac": "1C:C3:AB:01:61:D4", "sn": "SN1",
         "device_key": "medidor-a", "status": "RUNNING"}
DEV_B = {"tty_name": "ttyUSB0", "port_tcp": 5000, "mac": "AA:BB:CC:DD:EE:FF", "sn": "SN2",
         "device_key": "medidor-b", "status": "RUNNING"}


def bench(name, *devices, ok=True, url=None, error=None, id=None, location=None):
    return Bench(name=name, url=url or f"http://{name}:8080", address=name, port=8080, source="tailscale",
                 version="0.14.0", ok=ok, error=error, devices=list(devices), id=id, location=location)


class Clock:
    def __init__(self):
        self.t = "2026-10-06T10:00:00"

    def __call__(self):
        return self.t


def run(coro):
    return asyncio.run(coro)


# ---------- BenchCache ----------

def test_cache_exposes_the_bench_location_in_benches_and_devices():
    cache = BenchCache(scan=None, now=Clock())
    ba = {"label": "Buenos Aires, AR", "city": "Buenos Aires", "country": "AR", "source": "auto"}
    cache.update([bench("pi1", DEV_A, location=ba), bench("pi2", DEV_B)])
    s = {st.bench.name: st.summary() for st in cache.states()}
    assert (s["pi1"]["location"], s["pi2"]["location"]) == (ba, None)
    assert [(d["bench"], d["bench_location"]) for d in cache.devices()] == [("pi1", "Buenos Aires, AR"),
                                                                            ("pi2", None)]
    cache.update([bench("pi1", DEV_A, location={"label": "Lab Chile", "source": "manual"})])   # override en el bench
    assert cache.get("pi1").summary()["location"]["label"] == "Lab Chile"


def test_cache_keeps_offline_bench_with_last_snapshot():
    clock = Clock()
    cache = BenchCache(scan=None, now=clock)
    cache.update([bench("pi1", DEV_A), bench("pi2", DEV_B)])
    clock.t = "2026-10-06T10:05:00"
    cache.update([bench("pi1", DEV_A)])          # pi2 dejó de contestar

    s = {st.bench.name: st.summary() for st in cache.states()}
    assert s["pi1"]["online"] and s["pi1"]["last_seen"] == "2026-10-06T10:05:00"
    assert not s["pi2"]["online"] and s["pi2"]["last_seen"] == "2026-10-06T10:00:00"
    assert s["pi2"]["error"] == "no responde" and s["pi2"]["device_count"] == 1
    assert [st.bench.name for st in cache.states()] == ["pi1", "pi2"]   # online primero

    devs = cache.devices()
    assert [(d["device_key"], d["bench"], d["bench_online"]) for d in devs] == \
        [("medidor-a", "pi1", True), ("medidor-b", "pi2", False)]
    assert devs[0]["bench_url"] == "http://pi1:8080"

    cache.update([bench("pi1", DEV_A), bench("pi2", DEV_B)])   # vuelve
    assert cache.get("pi2").online and cache.get("pi2").error is None


def test_cache_bench_renamed_replaces_old_name():
    """sensipi03 pasa a llamarse dev (bench_name): misma URL, otro nombre. No tiene
    que quedar sensipi03 caído con sus placas viejas duplicadas."""
    cache = BenchCache(scan=None, now=Clock())
    cache.update([bench("sensipi03", DEV_A, url="http://100.1.1.3:8080"), bench("pi2", DEV_B)])
    cache.update([bench("dev", DEV_A, url="http://100.1.1.3:8080"), bench("pi2", DEV_B)])
    assert [st.bench.name for st in cache.states()] == ["dev", "pi2"]
    assert [d["bench"] for d in cache.devices()] == ["dev", "pi2"]


def test_cache_same_host_renamed_and_seen_by_another_address():
    """Por MAC del host: renombrado y visto por otra URL (LAN → Tailscale) es el mismo bench."""
    cache = BenchCache(scan=None, now=Clock())
    cache.update([bench("sensipi03", DEV_A, url="http://192.168.1.20:8080", id="dc:a6:32:00:00:01")])
    cache.update([bench("dev", DEV_A, url="http://100.1.1.3:8080", id="dc:a6:32:00:00:01")])
    assert [(st.bench.name, st.online) for st in cache.states()] == [("dev", True)]
    assert cache.get("dev") is not None and cache.get("sensipi03") is None


def test_cache_legacy_bench_upgraded_to_id():
    cache = BenchCache(scan=None, now=Clock())
    cache.update([bench("sensipi03", DEV_A, url="http://100.1.1.3:8080")])                      # sin id
    cache.update([bench("sensipi03", DEV_A, url="http://100.1.1.3:8080", id="dc:a6:32:00:00:01")])
    assert len(cache.states()) == 1 and cache.states()[0].bench.id == "dc:a6:32:00:00:01"


def test_cache_two_hosts_same_name_get_distinct_names():
    cache = BenchCache(scan=None, now=Clock())
    cache.update([bench("raspberrypi", DEV_A, url="http://10.0.0.1:8080", id="aa:00:00:00:00:01"),
                  bench("raspberrypi", DEV_B, url="http://10.0.0.2:8080", id="aa:00:00:00:00:02")])
    names = sorted(st.bench.name for st in cache.states())
    assert names == ["raspberrypi-0001", "raspberrypi-0002"]
    assert cache.get("raspberrypi-0002").bench.devices == [DEV_B]


def test_cache_devices_failure_keeps_previous_snapshot():
    cache = BenchCache(scan=None, now=Clock())
    cache.update([bench("pi1", DEV_A)])
    cache.update([bench("pi1", ok=False, error="/api/devices: timeout")])
    st = cache.get("pi1")
    assert not st.online and st.error == "/api/devices: timeout" and st.bench.devices == [DEV_A]


def test_cache_first_seen_broken():
    cache = BenchCache(scan=None, now=Clock())
    cache.update([bench("pi1", ok=False, error="x")])
    assert cache.get("pi1").last_seen is None and not cache.get("pi1").online


def test_refresh_runs_scan():
    cache = BenchCache(scan=lambda: [bench("pi1", DEV_A)], now=Clock())
    run(cache.refresh())
    assert cache.get("pi1").online and cache.last_scan == "2026-10-06T10:00:00"


# ---------- API propia ----------

def master_client(cache, **kw):
    app = create_app(cache=cache, poll_s=0, **kw)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8090")


def _cache(*found):
    c = BenchCache(scan=lambda: list(found), now=Clock())
    c.update(list(found))
    return c


def test_api_benches_devices_version():
    async def go():
        async with master_client(_cache(bench("pi1", DEV_A), bench("pi2", DEV_B))) as c:
            bs = (await c.get("/api/benches")).json()
            ds = (await c.get("/api/devices")).json()
            v = (await c.get("/api/version")).json()
            rs = (await c.post("/api/rescan")).json()
        return bs, ds, v, rs
    bs, ds, v, rs = run(go())
    assert [b["name"] for b in bs["benches"]] == ["pi1", "pi2"] and bs["last_scan"]
    assert {d["bench"] for d in ds} == {"pi1", "pi2"}
    assert v["app"] == "bench-master"
    assert rs == {"ok": True, "last_scan": "2026-10-06T10:00:00", "benches": 2}


def test_api_resolve():
    cache = _cache(bench("pi1", DEV_A), bench("pi2", DEV_B))

    async def go():
        async with master_client(cache) as c:
            return [await c.get(u) for u in ("/api/resolve/medidor-b", "/api/resolve/aabbccddeeff",
                                             "/api/resolve/pi1/esp-slot1", "/api/resolve/nada")]
    ok, mac, slash, missing = run(go())
    assert ok.status_code == 200
    assert ok.json() == {"bench": "pi2", "url": "http://pi2:8080", "address": "pi2", "port_tcp": 5000,
                         "tty_name": "ttyUSB0", "device": DEV_B}
    assert mac.json()["bench"] == "pi2" and slash.json()["tty_name"] == "esp-slot1"
    assert missing.status_code == 404


def test_api_resolve_ambiguous_and_offline():
    cache = _cache(bench("pi1", DEV_A), bench("pi2", DEV_A))

    async def go():
        async with master_client(cache) as c:
            amb = await c.get("/api/resolve/medidor-a")
            cache.update([bench("pi1", DEV_A)])    # pi2 offline: ya no compite
            one = await c.get("/api/resolve/medidor-a")
            cache.update([])
            none = await c.get("/api/resolve/medidor-a")
            return amb, one, none
    amb, one, none = run(go())
    assert amb.status_code == 409 and amb.json()["detail"]["matches"] == ["pi1/esp-slot1", "pi2/esp-slot1"]
    assert one.json()["bench"] == "pi1"
    assert none.status_code == 404


def test_frontend_served_with_shared_assets():
    async def go():
        async with master_client(_cache()) as c:
            return await c.get("/"), await c.get("/shared/espbench.js"), await c.get("/shared/style.css")
    index, js, css = run(go())
    assert index.status_code == 200 and "bench-master" in index.text
    assert js.status_code == 200 and "summarize" in js.text and css.status_code == 200
    assert index.headers["cache-control"] == "no-cache"


# ---------- proxy HTTP (bench falso por ASGI) ----------

def fake_bench_app():
    app = FastAPI()

    @app.get("/api/version")
    async def version():
        return {"app": "espbench", "version": "0.14.0", "name": "pi1"}

    @app.post("/api/device/{tty}/send")
    async def send(tty: str, request: Request, body: dict = Body(...)):
        return {"tty": tty, "body": body, "q": request.url.query, "host": request.headers.get("host"),
                "x": request.headers.get("x-test")}

    @app.get("/api/device/{tty}/sessions/{name}")
    async def session(tty: str, name: str):
        return PlainTextResponse("log\n", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/old")
    async def old():
        return RedirectResponse("/new")

    @app.get("/boom")
    async def boom():
        return PlainTextResponse("nope", status_code=409)

    return app


def test_proxy_forwards_method_body_query_headers():
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_bench_app()))

    async def go():
        async with master_client(_cache(bench("pi1", DEV_A)), http=http) as c:
            send = await c.post("/bench/pi1/api/device/esp-slot1/send?x=1", json={"text": "help"},
                                headers={"X-Test": "ok"})
            dl = await c.get("/bench/pi1/api/device/esp-slot1/sessions/output.log")
            redir = await c.get("/bench/pi1/old")
            err = await c.get("/bench/pi1/boom")
            root = await c.get("/bench/pi1")
            unknown = await c.get("/bench/nope/api/version")
            return send, dl, redir, err, root, unknown
    send, dl, redir, err, root, unknown = run(go())
    assert send.status_code == 200
    assert send.json() == {"tty": "esp-slot1", "body": {"text": "help"}, "q": "x=1", "host": "pi1:8080", "x": "ok"}
    assert dl.text == "log\n" and "output.log" in dl.headers["content-disposition"]
    assert redir.status_code == 307 and redir.headers["location"] == "/bench/pi1/new"
    assert err.status_code == 409 and err.text == "nope"
    assert root.status_code == 307 and root.headers["location"] == "/bench/pi1/"
    assert unknown.status_code == 404


def test_proxy_bench_down_is_502():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))

    async def go():
        async with master_client(_cache(bench("pi1")), http=http) as c:
            return await c.get("/bench/pi1/api/devices")
    r = run(go())
    assert r.status_code == 502 and "pi1" in r.json()["detail"]


def test_proxy_serves_real_bench_frontend_with_relative_urls():
    """El dashboard real de un bench (server.api) a través del proxy: las URLs son
    relativas, así que quedan bajo /bench/<nombre>/."""
    from server import api as bench_api
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=bench_api.app))

    async def go():
        async with master_client(_cache(bench("pi1")), http=http) as c:
            return (await c.get("/bench/pi1/"), await c.get("/bench/pi1/device.html?tty=esp-slot1"),
                    await c.get("/bench/pi1/espbench.js"), await c.get("/bench/pi1/api/version"))
    index, device, js, version = run(go())
    assert index.status_code == 200 and "getJson('api/devices')" in index.text
    assert 'href="/' not in index.text and 'href="/' not in device.text
    assert "EB.wsUrl(location, 'ws/device/'" in device.text
    assert js.status_code == 200
    assert version.json()["app"] == "espbench"


def test_note_and_props_reach_the_master_and_the_bench_catalog_through_the_proxy():
    """Las placas llegan con note/props (el master no las filtra), el catálogo de cada bench
    se lee por el proxy (bench/<n>/api/properties) y el frontend del master los dibuja."""
    from server import api as bench_api
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=bench_api.app))
    dev = {**DEV_A, "note": "dev ana", "note_by": "ana", "note_at": "2026-10-06T10:00:00-03:00",
           "props": {"chip": "esp32-s3", "estado": "no-tocar"}}

    async def go():
        async with master_client(_cache(bench("pi1", dev)), http=http) as c:
            return ((await c.get("/api/devices")).json(), (await c.get("/bench/pi1/api/properties")).json(),
                    (await c.get("/")).text, (await c.get("/bench/pi1/")).text)
    devices, props, master_index, bench_index = run(go())
    assert devices[0]["note"] == "dev ana" and devices[0]["props"] == {"chip": "esp32-s3", "estado": "no-tocar"}
    assert [c["id"] for c in props["categories"]][:3] == ["estado", "uso", "chip"]
    # los chips los dibuja EB.boardCardHtml (espbench.js) con la unión de los catálogos
    assert "EB.mergeCatalogs" in master_index and "'api/properties'" in master_index and "catalog: catalog" in master_index
    assert 'src="meta.js"' in bench_index            # relativo: anda bajo /bench/<n>/


def test_rejects_dns_rebinding_and_cross_site_writes():
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_bench_app()))

    async def go():
        async with master_client(_cache(bench("pi1")), http=http) as c:
            send = "/bench/pi1/api/device/esp-slot1/send"
            return {
                "rebind": await c.get("/api/devices", headers={"Host": "evil.example:8090"}),
                "csrf": await c.post(send, json={"text": "x"}, headers={"Origin": "https://evil.example"}),
                "same": await c.post(send, json={"text": "x"}, headers={"Origin": "http://localhost:8090"}),
                "no_origin": await c.post(send, json={"text": "x"}),
                "xs_get": await c.get("/api/version", headers={"Origin": "https://evil.example"}),
                "ipv6": await c.get("/api/version", headers={"Host": "[::1]:8090"}),
            }
    r = run(go())
    assert r["rebind"].status_code == 403 and r["csrf"].status_code == 403
    assert r["same"].status_code == 200 and r["no_origin"].status_code == 200
    assert r["xs_get"].status_code == 200 and r["ipv6"].status_code == 200


def test_allowed_hosts_none_skips_host_check():
    app = create_app(cache=_cache(), poll_s=0, allowed_hosts=None)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mi-mac.lan:8090") as c:
            return await c.get("/api/version"), await c.post("/api/rescan", headers={"Origin": "https://evil.example"})
    ok, csrf = run(go())
    assert ok.status_code == 200 and csrf.status_code == 403


# ---------- proxy WebSocket ----------

TC_HOST = {"testserver"}   # el TestClient de WebSocket siempre manda Host: testserver


class FakeUpstream:
    """Lado bench del WebSocket: manda `initial` y hace eco de lo que recibe."""

    def __init__(self, initial):
        self.initial = initial
        self.sent = []
        self.url = None

    async def __aenter__(self):
        self.q = asyncio.Queue()
        for m in self.initial:
            self.q.put_nowait(m)
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        m = await self.q.get()
        if m is None:
            raise StopAsyncIteration
        return m

    async def send(self, m):
        self.sent.append(m)
        await self.q.put("eco:" + m)


def test_ws_proxy_both_directions():
    up = FakeUpstream(["hola\n", b"\x00bin"])

    def connect(url, **kw):
        up.url = url
        up.kw = kw
        return up

    app = create_app(cache=_cache(bench("pi1", url="http://100.1.2.3:8080")), poll_s=0, ws_connect=connect, allowed_hosts=TC_HOST)
    with TestClient(app).websocket_connect("/bench/pi1/ws/device/esp-slot1") as ws:
        assert ws.receive_text() == "hola\n"
        assert ws.receive_bytes() == b"\x00bin"
        ws.send_text("ping")
        assert ws.receive_text() == "eco:ping"
    assert up.url == "ws://100.1.2.3:8080/ws/device/esp-slot1"
    assert up.sent == ["ping"]


def test_ws_proxy_rejects_cross_site_origin():
    up = FakeUpstream(["x"])
    app = create_app(cache=_cache(bench("pi1")), poll_s=0, ws_connect=lambda url, **kw: up, allowed_hosts=TC_HOST)
    with pytest.raises(Exception):
        with TestClient(app).websocket_connect(
                "/bench/pi1/ws/device/x", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_text()
    assert up.url is None


def test_ws_proxy_unknown_bench_closes():
    app = create_app(cache=_cache(), poll_s=0, allowed_hosts=TC_HOST)
    with pytest.raises(Exception):
        with TestClient(app).websocket_connect("/bench/nope/ws/device/x") as ws:
            ws.receive_text()


def test_ws_proxy_upstream_down_closes():
    def connect(url, **kw):
        raise OSError("refused")
    app = create_app(cache=_cache(bench("pi1")), poll_s=0, ws_connect=connect, allowed_hosts=TC_HOST)
    with TestClient(app).websocket_connect("/bench/pi1/ws/device/x") as ws:
        with pytest.raises(Exception):
            ws.receive_text()


# ---------- de punta a punta: bench y master reales en uvicorn ----------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, "uvicorn no arrancó"
        time.sleep(0.05)
    return server


def test_end_to_end_real_servers():
    """Bench falso y master reales (uvicorn), discovery real (client.benches.scan por
    config), proxy HTTP con httpx y WS con websockets de verdad."""
    import websockets.sync.client as wsc
    from client import benches

    bench_app = fake_bench_app()

    @bench_app.get("/api/devices")
    async def devices():
        return [DEV_A]

    @bench_app.websocket("/ws/device/{tty}")
    async def log(ws: WebSocket, tty: str):
        await ws.accept()
        await ws.send_text(f"log de {tty}\n")
        await ws.send_text("eco:" + await ws.receive_text())
        await ws.close()

    bport, mport = _free_port(), _free_port()
    cfg = {"hosts": [f"127.0.0.1:{bport}"], "tailscale": False, "timeout_s": 2}
    bsrv = _serve(bench_app, bport)
    msrv = _serve(create_app(cache=BenchCache(scan=lambda: benches.scan(cfg)), poll_s=0.2), mport)
    try:
        base = f"http://127.0.0.1:{mport}"
        deadline = time.time() + 10
        while not httpx.get(base + "/api/benches").json()["benches"]:
            assert time.time() < deadline, "el master no encontró el bench"
            time.sleep(0.1)
        assert httpx.get(base + "/api/devices").json()[0]["bench"] == "pi1"
        assert httpx.get(base + "/api/resolve/medidor-a").json()["port_tcp"] == 5001
        assert httpx.get(base + "/bench/pi1/api/version").json()["name"] == "pi1"
        with wsc.connect(f"ws://127.0.0.1:{mport}/bench/pi1/ws/device/esp-slot1", open_timeout=5) as ws:
            assert ws.recv(timeout=5) == "log de esp-slot1\n"
            ws.send("hola")
            assert ws.recv(timeout=5) == "eco:hola"
    finally:
        msrv.should_exit = True
        bsrv.should_exit = True

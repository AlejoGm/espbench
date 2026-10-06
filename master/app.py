"""
app.py — bench-master: todos los benches de espbench en un solo lugar.

Corre local en la máquina del dev (127.0.0.1:8090). No guarda nada en disco:
pollea los benches que encuentra client/benches.py (Tailscale + config) y arma

- GET  /api/benches         estado de cada bench (online, versión, visto hace...)
- GET  /api/devices         los devices de todos los benches, con su campo `bench`
- GET  /api/resolve/{key}   en qué bench está una placa (device_key, SN, MAC, <bench>/<tty>)
- POST /api/rescan          buscar benches ya, sin esperar al próximo poll
- /bench/<nombre>/...       proxy HTTP + WebSocket al dashboard de ese bench: su
                            device.html (log en vivo, consola, historial) a través
                            del master, sin conectarse a cada bench

Un bench que deja de contestar queda en la lista como offline, con el último
snapshot de sus devices (en memoria, se pierde al reiniciar el master).
"""
import asyncio
import dataclasses
import datetime
import pathlib
import sys
from typing import Callable, Dict, List, Optional, Set
from urllib.parse import urlsplit

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

from client import benches  # noqa: E402
from client.benches import Bench  # noqa: E402

DASHBOARD_DIR = pathlib.Path(__file__).resolve().parent / "dashboard"
SHARED_DIR = REPO_DIR / "remote" / "dashboard"   # espbench.js + style.css del dashboard de los benches

# Headers que no se reenvían: hop-by-hop, y los que httpx recalcula (descomprime el body).
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "host", "content-length", "content-encoding"}


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _hostname(host_header: str) -> str:
    """"localhost:8090" → "localhost", "[::1]:8090" → "::1"."""
    try:
        return urlsplit("//" + host_header).hostname or ""
    except ValueError:
        return ""


class _OnlyFromThisPage:
    """El proxy le da a cualquiera que llegue al master la consola serie y los resets
    de todos los benches. Escuchando en localhost alcanza con que llegue el browser
    del dev, así que se cierran dos caminos para que una página web cualquiera lo use:

    - DNS rebinding: un dominio ajeno que resuelve a 127.0.0.1. Se rechaza todo Host
      que no esté en `allowed` (None = sin chequeo, para --host 0.0.0.0).
    - CSRF: un form o fetch cross-site. Escrituras (todo lo que no es GET/HEAD) y
      WebSockets con un Origin distinto del propio master se rechazan."""

    def __init__(self, app, allowed: Optional[Set[str]]):
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            host = headers.get("host", "")
            origin = headers.get("origin")
            unsafe = scope["type"] == "websocket" or scope["method"] not in ("GET", "HEAD", "OPTIONS")
            reason = None
            if self.allowed is not None and _hostname(host) not in self.allowed:
                reason = f"host no permitido: {host}"
            elif unsafe and origin and urlsplit(origin).netloc != host:
                reason = f"origin no permitido: {origin}"
            if reason:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4403})
                else:
                    await PlainTextResponse(reason, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _now_iso() -> str:
    # Hora local sin zona, como los timestamps de los benches (EB.relTime los entiende igual).
    return datetime.datetime.now().replace(microsecond=0).isoformat()


@dataclasses.dataclass
class BenchState:
    bench: Bench                 # último snapshot bueno (devices incluidos)
    online: bool
    last_seen: Optional[str]     # último poll que contestó bien
    error: Optional[str] = None

    def summary(self) -> dict:
        b = self.bench
        return {"name": b.name, "id": b.id, "location": b.location, "url": b.url, "address": b.address,
                "port": b.port, "source": b.source,
                "version": b.version, "online": self.online, "last_seen": self.last_seen,
                "error": self.error, "device_count": len(b.devices)}


class BenchCache:
    """Estado de todos los benches conocidos, por nombre. Se actualiza con cada scan."""

    def __init__(self, scan: Callable[[], List[Bench]] = benches.scan, now: Callable[[], str] = _now_iso):
        self._scan = scan
        self._now = now
        self._states: Dict[str, BenchState] = {}
        self.last_scan: Optional[str] = None

    def update(self, found: List[Bench]) -> None:
        """Por identidad del bench (MAC del host, `Bench.key`), no por nombre: un bench
        renombrado sigue siendo el mismo, con el nombre nuevo."""
        now = self._now()
        seen = set()
        for b in found:
            k = b.key
            seen.add(k)
            # El mismo host bajo otra clave: un bench viejo (sin id, por nombre) que se
            # actualizó o se renombró contesta desde la misma URL. Si no se borra, queda
            # como caído con la foto vieja de sus placas (duplicadas en la grilla).
            for old in [o for o, st in self._states.items() if o != k and st.bench.url == b.url]:
                del self._states[old]
            prev = self._states.get(k)
            if b.ok:
                self._states[k] = BenchState(b, online=True, last_seen=now)
            elif prev:
                # Contestó /api/version pero no /api/devices: se queda el snapshot anterior.
                prev.online, prev.error = False, b.error
            else:
                self._states[k] = BenchState(b, online=False, last_seen=None, error=b.error)
        for k, st in self._states.items():
            if k not in seen:
                st.online, st.error = False, "no responde"
        self._unique_names()
        self.last_scan = now

    def _unique_names(self) -> None:
        """El nombre va en las URLs (/bench/<nombre>/): dos máquinas que se llaman igual
        (el hostname por defecto, por ejemplo) se distinguen con el final de su MAC."""
        by_name: Dict[str, List[BenchState]] = {}
        for st in self._states.values():
            by_name.setdefault(st.bench.name, []).append(st)
        for name, group in by_name.items():
            if len(group) > 1:
                for st in group:
                    if st.bench.id:
                        st.bench.name = f"{name}-{st.bench.id.replace(':', '')[-4:]}"

    async def refresh(self) -> None:
        self.update(await asyncio.get_running_loop().run_in_executor(None, self._scan))

    def get(self, name: str) -> Optional[BenchState]:
        for st in self._states.values():
            if st.bench.name == name:
                return st
        return None

    def states(self) -> List[BenchState]:
        # Online primero, después por nombre.
        return sorted(self._states.values(), key=lambda s: (not s.online, s.bench.name))

    def devices(self) -> List[dict]:
        out = []
        for st in self.states():
            for d in st.bench.devices:
                out.append(dict(d, bench=st.bench.name, bench_url=st.bench.url, bench_online=st.online,
                                bench_last_seen=st.last_seen, bench_location=benches.location_label(st.bench.location)))
        return out


class _NoCacheStatic(StaticFiles):
    """Estáticos con revalidación en cada carga (como en los benches): después de un
    pull no queda JS/CSS viejo en el navegador."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _version() -> str:
    try:
        return (REPO_DIR / "VERSION").read_text().strip()
    except OSError:
        return "dev"


def create_app(cache: Optional[BenchCache] = None, poll_s: float = 5.0,
               http: Optional[httpx.AsyncClient] = None, ws_connect: Callable = websockets.connect,
               allowed_hosts: Optional[Set[str]] = LOCAL_HOSTS) -> FastAPI:
    """`poll_s=0` no arranca el poll (tests). `http` y `ws_connect` son inyectables para
    probar el proxy contra un bench falso. `allowed_hosts`: ver _OnlyFromThisPage."""
    cache = cache if cache is not None else BenchCache()
    http = http if http is not None else httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    app = FastAPI(title="bench-master")
    app.state.cache = cache
    app.add_middleware(_OnlyFromThisPage, allowed=allowed_hosts)

    async def _poll():
        while True:
            try:
                await cache.refresh()
            except RuntimeError:     # el loop se está cerrando (shutdown)
                return
            except Exception as e:   # un scan roto no puede matar el poll
                print(f"[bench-master] scan falló: {e}", file=sys.stderr)
            await asyncio.sleep(poll_s)

    @app.on_event("startup")
    async def _startup():
        if poll_s > 0:
            app.state.poller = asyncio.create_task(_poll())

    @app.on_event("shutdown")
    async def _shutdown():
        task = getattr(app.state, "poller", None)
        if task:
            task.cancel()
        await http.aclose()

    # ---------- API propia ----------

    @app.get("/api/version")
    async def version():
        return {"app": "bench-master", "version": _version()}

    @app.get("/api/benches")
    async def list_benches():
        return {"last_scan": cache.last_scan, "benches": [s.summary() for s in cache.states()]}

    @app.get("/api/devices")
    async def list_devices():
        return cache.devices()

    @app.post("/api/rescan")
    async def rescan():
        await cache.refresh()
        return {"ok": True, "last_scan": cache.last_scan, "benches": len(cache.states())}

    @app.get("/api/resolve/{key:path}")
    async def resolve(key: str):
        """Solo entre benches online: una placa en un bench caído no se puede flashear."""
        online = [s.bench for s in cache.states() if s.online]
        hits = benches.find(key, online)
        if not hits:
            raise HTTPException(status_code=404, detail=f"'{key}' no está en ningún bench online")
        if len(hits) > 1:
            raise HTTPException(status_code=409, detail={
                "error": "ambiguous", "matches": [f"{b.name}/{d.get('tty_name')}" for b, d in hits]})
        b, d = hits[0]
        return {"bench": b.name, "url": b.url, "address": b.address, "port_tcp": d.get("port_tcp"),
                "tty_name": d.get("tty_name"), "device": d}

    # ---------- proxy a los benches ----------

    def _state_or_404(name: str) -> BenchState:
        st = cache.get(name)
        if st is None:
            raise HTTPException(status_code=404, detail=f"bench '{name}' desconocido")
        return st

    @app.get("/bench/{name}")
    async def bench_root(name: str):
        _state_or_404(name)
        return RedirectResponse(f"/bench/{name}/")

    @app.api_route("/bench/{name}/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(name: str, path: str, request: Request):
        st = _state_or_404(name)
        url = f"{st.bench.url}/{path}"
        if request.url.query:
            url += "?" + request.url.query
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        try:
            r = await http.request(request.method, url, headers=headers, content=await request.body())
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"bench '{name}' no responde: {e}")
        out = {k: v for k, v in r.headers.items() if k.lower() not in _HOP}
        loc = r.headers.get("location")
        if loc and loc.startswith("/"):
            out["location"] = f"/bench/{name}{loc}"
        return Response(content=r.content, status_code=r.status_code, headers=out)

    @app.websocket("/bench/{name}/ws/{path:path}")
    async def ws_proxy(ws: WebSocket, name: str, path: str):
        st = cache.get(name)
        if st is None:
            await ws.close(code=4404)
            return
        target = "ws" + st.bench.url[len("http"):] + "/ws/" + path
        await ws.accept()
        try:
            async with ws_connect(target, open_timeout=5, max_size=None) as upstream:
                async def downstream():
                    async for msg in upstream:
                        if isinstance(msg, bytes):
                            await ws.send_bytes(msg)
                        else:
                            await ws.send_text(msg)

                async def upstream_pump():
                    while True:
                        m = await ws.receive()
                        if m["type"] == "websocket.disconnect":
                            return
                        if m.get("text") is not None:
                            await upstream.send(m["text"])
                        elif m.get("bytes") is not None:
                            await upstream.send(m["bytes"])

                tasks = [asyncio.ensure_future(downstream()), asyncio.ensure_future(upstream_pump())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                for t in done:
                    t.exception()   # consumir: un corte de cualquiera de los dos lados es normal
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException):
            pass
        finally:
            try:
                await ws.close()
            except RuntimeError:
                pass   # el browser ya cerró

    # ---------- frontend ----------
    app.mount("/shared", _NoCacheStatic(directory=str(SHARED_DIR)), name="shared")
    app.mount("/", _NoCacheStatic(directory=str(DASHBOARD_DIR), html=True), name="static")
    return app

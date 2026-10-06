# master/ — bench-master

Todos los benches de espbench en un solo lugar. Corre **local, en la máquina del dev** (no en un bench).
Un bench es cualquier host con el dashboard de espbench (`remote/server/api.py`, :8080): una Pi u otra máquina.

```bash
master/bench-master --open        # arma master/.venv la primera vez; http://localhost:8090
```

| Archivo | |
|---|---|
| `app.py` | FastAPI: `BenchCache` (estado de los benches, en memoria), API propia, proxy HTTP + WS, guard de Host/Origin |
| `__main__.py` | `python -m master`: `--host` (default 127.0.0.1), `--port` (8090), `--poll` (5 s), `--open` |
| `bench-master` | Launcher: crea/actualiza `.venv` desde `requirements.txt` y corre `python -m master` |
| `dashboard/index.html` | Grilla de todos los devices agrupada por bench. Reusa `espbench.js` y `style.css` de `remote/dashboard/` (montados en `/shared`). Cada card con la nota y las propiedades (chips; click filtra `cat:valor`, la búsqueda también mira la nota); los chips se colorean con la unión de los catálogos de los benches (`bench/<n>/api/properties` por el proxy, `EB.mergeCatalogs`). **Solo lectura a propósito**: nota y propiedades se editan en el dashboard de cada bench (también a través del proxy, `/bench/<n>/`), que tiene su token y su catálogo; el master solo las muestra, filtra y cuenta (`st-avoid`) |

## Cómo funciona

- **Discovery**: `client/benches.py` (Tailscale + `~/.config/espbench-benches.json`), cada `--poll` segundos en un thread.
- **`BenchCache`**: por nombre de bench. Un bench que deja de contestar queda `online: false` con el **último snapshot** de sus devices y `last_seen`. Nada en disco: al reiniciar el master se pierde.
- **API**: `GET /api/benches`, `GET /api/devices` (cada device con `bench`, `bench_url`, `bench_online`), `GET /api/resolve/{key}` (solo benches online; 404 / 409 si es ambiguo), `POST /api/rescan`, `GET /api/version`.
- **Proxy**: `/bench/<nombre>/<path>` → `http://<bench>:8080/<path>` (todos los métodos), `/bench/<nombre>/ws/<path>` → WebSocket del bench. Así el `device.html` del bench (log en vivo, consola, historial) anda a través del master. Requiere benches con el frontend de URLs relativas (≥ 0.13.0); los viejos se abren con el link directo `↗`.
- **Update de un bench**: botón **⟳ update** en la cabecera de cada bench → `POST /bench/<n>/api/update` por el proxy (ref vacía = último release). Muestra el PIN (📌) y el resultado del último update (`GET /bench/<n>/api/update`). El token del bench se guarda con la misma clave que usa `auth.js` del bench (`eb.apiToken:/bench/<n>/`).
- **Guard** (`_OnlyFromThisPage`): el proxy da consola serie y resets de todos los benches sin auth. Se rechaza (403) un `Host` que no sea localhost (DNS rebinding) y cualquier escritura o WebSocket con `Origin` ajeno (CSRF). Con `--host 0.0.0.0` no hay chequeo de Host: cualquiera en la red llega a las consolas.

## Tests

`tests/test_master.py` necesita httpx y websockets; sin ellos se saltea. Con el venv del master:

```bash
master/.venv/bin/python -m pytest tests/
```

Cubre la cache (offline con snapshot), la API, el proxy contra un bench falso por ASGI y contra el `server.api` real (frontend con URLs relativas), el guard, y un punta a punta con bench y master reales en uvicorn.

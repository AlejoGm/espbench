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
| `dashboard/index.html` | Card protagonista del bench elegido (salud de la máquina, reinicios por hora en 24 h con los panics marcados, Actualizar), lista de benches, alertas con notificaciones del sistema y la grilla de placas de todos los benches. Reusa `espbench.js`, `theme.js` y `style.css` de `remote/dashboard/` (montados en `/shared`). Cada card (`EB.boardCardHtml`) con la nota y las propiedades (chips; click filtra `cat:valor`, la búsqueda también mira la nota); los chips se colorean con la unión de los catálogos de los benches (`EB.mergeCatalogs`). **Solo lectura a propósito**: nota y propiedades se editan en el dashboard de cada bench (también a través del proxy, `/bench/<n>/`), que tiene su token y su catálogo; el master solo las muestra, filtra y separa en la pestaña "No tocar / rotas" (estado `avoid`) |

## Cómo funciona

- **Discovery**: `client/benches.py` (Tailscale + `~/.config/espbench-benches.json`), cada `--poll` segundos en un thread.
- **`BenchCache`**: por identidad del bench (MAC del host, `Bench.key`), no por nombre: renombrado o con otra IP sigue siendo el mismo. Un bench viejo sin `id` va por nombre, y si aparece con `id` en la misma URL lo reemplaza. Nombres repetidos entre máquinas distintas llevan el final de la MAC. Un bench que deja de contestar queda `online: false` con el **último snapshot** de sus devices y `last_seen`. Nada en disco: al reiniciar el master se pierde.
- **API**: `GET /api/benches`, `GET /api/devices` (cada device con `bench`, `bench_url`, `bench_online`), `GET /api/resolve/{key}` (solo benches online; 404 / 409 si es ambiguo), `POST /api/rescan`, `GET /api/version`.
- **Proxy**: `/bench/<nombre>/<path>` → `http://<bench>:8080/<path>` (todos los métodos), `/bench/<nombre>/ws/<path>` → WebSocket del bench. Así el `device.html` del bench (log en vivo, consola, historial) anda a través del master. Requiere benches con el frontend de URLs relativas (≥ 0.13.0); los viejos se abren con el link directo `↗`.
- **Datos de cada bench**: el master solo pollea `/api/devices`; la página pide por el proxy, cada 30 s, `/api/bench/health`, `/api/activity`, `/api/update` y `/api/properties` (catálogo; un bench viejo no lo tiene) de cada bench online.
- **Alertas**: panics, boot loops y flashes de las últimas 24 h (`recent` de `/api/activity`), placas sin log y benches que no responden. Con el interruptor, notificación del sistema (Notification API, `localStorage` `eb.notify`) de lo que aparece **después** de abrir la página; nunca de un flash.
- **Update de un bench**: botón **Actualizar bench** de la card protagonista → `POST /bench/<n>/api/update` por el proxy (ref vacía = último release). Muestra el PIN (📌) y el resultado del último update (`GET /bench/<n>/api/update`). El token del bench se guarda con la misma clave que usa `auth.js` del bench (`eb.apiToken:/bench/<n>/`).
- **Guard** (`_OnlyFromThisPage`): el proxy da consola serie y resets de todos los benches sin auth. Se rechaza (403) un `Host` que no sea localhost (DNS rebinding) y cualquier escritura o WebSocket con `Origin` ajeno (CSRF). Con `--host 0.0.0.0` no hay chequeo de Host: cualquiera en la red llega a las consolas.

## Tests

`tests/test_master.py` necesita httpx y websockets; sin ellos se saltea. Con el venv del master:

```bash
master/.venv/bin/python -m pytest tests/
```

Cubre la cache (offline con snapshot), la API, el proxy contra un bench falso por ASGI y contra el `server.api` real (frontend con URLs relativas), el guard, y un punta a punta con bench y master reales en uvicorn.

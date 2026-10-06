# remote/dashboard/

Frontend del dashboard. Lo sirve `server/api.py` (montaje estático de FastAPI, con `Cache-Control: no-cache` para que después de un update no quede JS/CSS viejo). Vanilla HTML/CSS/JS: sin framework, sin build.

| Archivo | |
|---|---|
| `index.html` | Grilla de cards, una por device. Pollea `/api/devices` cada 5 s |
| `device.html` | Log del device en vivo (`/ws/device/{tty}`), historial, consola serie, botones reset/boot/sesión/unlock |
| `espbench.js` | Lógica sin DOM (`window.EB`): tiempos relativos, badges de salud, clasificación de líneas, ANSI → HTML, `LineBuffer`. Tests: `tests/js/test_espbench.js` (node) |
| `style.css` | Tema oscuro, grilla responsive |

Lo que se pueda testear sin navegador va en `espbench.js`, con su test en `tests/js/`. Las páginas solo arman DOM.

## index.html

- Header con contadores (devices, ok, ocupados, con problemas, caídos, bloqueados) y búsqueda (`/`): filtra por nombre, tty, SN, MAC, firmware, deployer, lock.
- Card por device: nombre (renombrable, `PATCH /api/devices/{mac}`), HW, firmware (`proyecto versión · IDF`), último flash relativo con ✓/✗ (de `result.json`) y deployer, SN, puerto.
- Franja izquierda de color: verde ok, ámbar reset anormal o lock, rojo panic/boot loop, azul pulsando flasheando/borrando/iniciando, gris sin MAC, rojo apagado caído.
- Badges de salud (`health`, de `SerialWatch`): **BOOT LOOP**, `⚠ N panics` (tooltip con el último), `↯ <reset anormal>`, `↻ N` reinicios. Se resetean al flashear.
- Badges de estado: `status` (RUNNING/DOWN) y `state` de la FSM (**FLASHEANDO** / **BORRANDO** / **INICIANDO** / **SIN MAC** / **DESCONECTADO**; `monitoring` no lleva badge).
- Los devices con MAC conocida van en la grilla principal; los que no, en "sin identificar".

## device.html

- `?tty=<nombre>`. WebSocket a `/ws/device/{tty}`: manda la sesión actual completa y después el stream. Al reconectar se limpia la vista (antes se duplicaba el log).
- **Render incremental**: cada línea es un `<div>`, la línea en curso se reescribe. Tope de 20 000 líneas en la vista (las viejas se recortan; el log completo se descarga con `⤓ Log`).
- Líneas de `taglog` (server) con `▸` y color por nivel; panics/backtrace resaltados en rojo; cada `rst:` marca un separador.
- Toolbar: filtro de texto (`/`), "solo problemas" (panics, resets, E/W de ESP-IDF, WARN/ERROR de taglog), ir al último panic, pausar/seguir (`End`), limpiar, descargar.
- Historial (panel lateral): flasheos con resultado, usuario y error (`/api/device/{tty}/jobs`, log de cada uno en un visor) y sesiones de log anteriores (`/sessions`, ver o descargar).
- Consola serie abajo (`i` para enfocar): `POST /api/device/{tty}/send`, con ⏎ opcional e historial con ↑↓. Se deshabilita mientras flashea/borra.
- Los badges (`/api/device/{tty}`) se refrescan cada 3 s.

## Notas

- Todas las URLs son **relativas** a la página (`api/...`, `device.html`, `style.css`; WebSocket con `EB.wsUrl`). Nada empieza con `/`: bench-master sirve este mismo frontend bajo `/bench/<nombre>/`.
- `espbench.js` también tiene la lógica de cards (`cardState`, `summarize`, `firmwareHtml`...) que reusa bench-master (`master/dashboard/`).
- No tiene autenticación: es solo para uso en la red interna. Ojo: la consola serie permite escribirle a cualquier device.
- Para probarlo sin Pi: levantar `server.api` con devices simulados en `run/<tty>.json` (necesita `uvicorn[standard]`, que es el que trae soporte de WebSocket).

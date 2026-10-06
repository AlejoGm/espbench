# remote/dashboard/

Frontend del dashboard. Lo sirve `server/api.py` (montaje estático de FastAPI, con `Cache-Control: no-cache` para que después de un update no quede JS/CSS viejo). Vanilla HTML/CSS/JS: sin framework, sin build.

| Archivo | |
|---|---|
| `index.html` | Grilla de cards, una por device. Pollea `/api/devices` cada 5 s |
| `device.html` | Log del device en vivo (`/ws/device/{tty}`) con marcas de eventos, historial (eventos, flasheos, sesiones), consola serie, reserva (liberar/forzar), botones reset/boot/sesión |
| `espbench.js` | Lógica sin DOM (`window.EB`): tiempos relativos, badges de salud, prefijo de línea (`splitPrefix`), clasificación de líneas, ANSI → HTML, `LineBuffer`; reservas (`lockInfo`, `expiresText`, `forceConfirmText`, `searchMatch`); eventos (`eventView`, `eventCounts`, `eventContext`, `findLine`, `lineMark`, `EventMarks`). Tests: `tests/js/test_espbench.js` (node) |
| `auth.js` | `EBAuth.fetch`: escrituras con el token de la API (`localStorage`); ante un 401 lo pide en una barra inline y reintenta |
| `style.css` | Tema oscuro, grilla responsive |

Lo que se pueda testear sin navegador va en `espbench.js`, con su test en `tests/js/`. Las páginas solo arman DOM.

## index.html

- Header con contadores (devices, ok, ocupados, con problemas, caídos, **reservadas** —tooltip con quién y hasta cuándo— y **con lock de flash**) y búsqueda (`/`): filtra por nombre, tty, SN, MAC, firmware, deployer, lock. `@usuario` filtra solo por el usuario del lock (`@` solo: cualquier placa con lock); click en un contador de locks o en el badge de una card lo completa.
- Card por device: nombre (renombrable, `PATCH /api/devices/{mac}`), HW, firmware (`proyecto versión · IDF`), último flash relativo con ✓/✗ (de `result.json`) y deployer, SN, puerto.
- Franja izquierda de color: verde ok, ámbar reset anormal o lock, rojo panic/boot loop, azul pulsando flasheando/borrando/iniciando, gris sin MAC, rojo apagado caído.
- Badges de salud (`health`, de `SerialWatch`): **BOOT LOOP**, `⚠ N panics` (tooltip con el último), `↯ <reset anormal>`, `↻ N` reinicios. Se resetean al flashear.
- Badges de estado: `status` (RUNNING/DOWN) y `state` de la FSM (**FLASHEANDO** / **BORRANDO** / **INICIANDO** / **SIN MAC** / **DESCONECTADO**; `monitoring` no lleva badge).
- Lock en la card: `🔒 user · vence en 20 min` (reserva) o `🔒 user · sin vencimiento` (lock del flash, más apagado), tooltip con la hora exacta. El "vence en" lo actualiza un tick de 1 s sin esperar el poll, y al vencer el badge desaparece (el server ya la ignora: vencida = inexistente).
- Los devices con MAC conocida van en la grilla principal; los que no, en "sin identificar".

## device.html

- `?tty=<nombre>`. WebSocket a `/ws/device/{tty}`: manda la sesión actual completa y después el stream. Al reconectar se limpia la vista (antes se duplicaba el log).
- **Render incremental**: cada línea es un `<div>`, la línea en curso se reescribe. Tope de 20 000 líneas en la vista (las viejas se recortan; el log completo se descarga con `⤓ Log`).
- Cada línea del log trae el prefijo del `DeviceLog`: `YYYY-MM-DD HH:MM:SS.mmm <origen> ` (`>` serial, `|` taglog, `↪` continuación de una línea serial partida). `splitPrefix` lo separa **antes** de `lineClass` (sus regex están ancladas con `^`) y de `overwrite` (que corta en el último `\r`). Las líneas sin prefijo (logs viejos) se ven como antes.
- Líneas de `taglog` (server) con `▸` y color por nivel; panics/backtrace resaltados en rojo; cada `rst:` marca un separador; las continuaciones con `↪`.
- Toolbar: filtro de texto (`/`, mira el cuerpo, no la hora), "solo problemas" (panics, resets, E/W de ESP-IDF, WARN/ERROR de taglog), ir al último panic, **Hora** (muestra/oculta la hora de cada línea; se recuerda en `localStorage`), pausar/seguir (`End`), limpiar, descargar.
- Historial (panel lateral): **eventos** (pestaña por defecto), flasheos con resultado, usuario y error (`/api/device/{tty}/jobs`, log de cada uno en un visor) y sesiones de log anteriores (`/sessions`, ver o descargar).
- **Eventos** (`/api/board/{MAC}/events`; la MAC sale de `/api/device/{tty}`, sin MAC no hay eventos): el más nuevo arriba, separados por sesión, con hora, ícono/color por tipo, detalle corto y quién (`detail.user`). Chips por tipo (conteo de los últimos cargados; filtran en el server con `type=`), "cargar más" sube `limit` de a 100 hasta 1000 (usa `more`). Click → visor con el contexto: `/log?around=<cursor>` (panic/boot_loop: del `rst:` anterior al siguiente; el resto `before=40&after=200`), `raw=1` para conservar colores. La línea del evento se pide aparte (`around=<cursor>&before=0&after=0`) y se busca en el contexto por texto con la hora en ms (`EB.findLine`): las líneas de `/log` no traen offset. Un evento del api al final del log (sin línea después) se marca con una nota al final. El cursor lleva la sesión: anda con sesiones anteriores; la descarga del visor es el archivo de esa sesión.
- **Marcas en el vivo** (al costado de la línea): `⚠` panic y `↻` boot por el contenido de la línea (`EB.lineMark`, la misma detección que `serial_watch.line_kind`: el inicio del panic, no el backtrace); `›` send, `⌘` command y `⚡` flash por eventos (`/events?type=send,command,flash&since=session` cada 4 s). El WebSocket manda texto sin offsets (y el tail puede saltear o repetir bytes entre el contenido inicial y el stream), así que los eventos del api se ubican **por hora**: la primera línea con hora ≥ la del evento − 500 ms (`EB.EventMarks`; el api registra el `send` después de mandar las teclas y el eco puede llegar antes). Es aproximado a propósito: una marca puede caer una línea antes o después. Las marcas no tocan el filtro ni el render incremental (se agregan a líneas completas, nunca a la línea en curso).
- Consola serie abajo (`i` para enfocar): `POST /api/device/{tty}/send`, con ⏎ opcional e historial con ↑↓. Se deshabilita mientras flashea/borra.
- Escrituras (`send`, `command`, `devremote-reset`) por `postJson`, con el par `lock_user`/`lock_token` recordado si hay (el dueño de la reserva escribe sin 423 y los eventos llevan su usuario). Ante un 423 relee el device y el `confirm` dice quién y hasta cuándo (`EB.forceConfirmText`, el mismo texto que el botón Forzar); si se acepta, reintenta con `force: true`. Los errores del API vienen como `{detail: "texto"}` o `{detail: {error, message}}`: mostrarlos con `EB.errorText`.
- **Reserva** en el header: badge `🔒 reserva | user · vence en X` (o `lock flash · sin vencimiento`), con tick de 1 s. **Liberar** (`/release`): con el par recordado del mismo usuario va directo; si no, lo pide en una barra inline (user prellenado, token, "recordar" → `localStorage` `eb.lockUser`/`eb.lockToken`); un 403 la vuelve a mostrar con el error. **Forzar** (`/unlock {force: true}`, con confirm, por `EBAuth.fetch`): suelta la reserva o el lock de otro y queda un `release` con el dueño anterior y quién forzó (`by_user`/`by_host`). Solo se muestra si `/api/version` dice `auth: true`: sin token de la API el server lo rechaza (403 `force_disabled`). El `force` de las escrituras (423) sigue andando sin token.
- Los badges (`/api/device/{tty}`) se refrescan cada 3 s.

## Notas

- Las llamadas usan el mismo host/puerto que la página (no hay URLs hardcodeadas).
- Autenticación opcional: si la Pi tiene `/opt/esp/api_token`, toda escritura va por `EBAuth.fetch` (si no, da 401). Las lecturas y el WebSocket siguen abiertos. Sin el archivo, la consola serie permite escribirle a cualquier device desde la red interna.
- Para probarlo sin Pi: `ESP_BASE=$(mktemp -d) python -m tests.benchsim --uvicorn` (placa simulada con eventos reales; necesita `uvicorn[standard]` o `websockets`: sin eso el WebSocket del vivo falla y el resto anda).

# Spec — espbench para agentes (CLI `espbench`, eventos, log con timestamps)

Estado: **v2, revisada** · rama `feat/agents` (sobre `feat/dashboard-ux`) · 2026-10-05

Sale de un grill con Alejo. v2 incorpora una revisión contra el código (sección 10). Lo marcado **[confirmar]** es propuesta, no decisión.

---

## 1. Objetivo

Que un agente (Claude Code en la máquina del dev) cierre el ciclo completo contra una placa real sin humano en el medio:

```
editar → build → flash → verificar arranque → mandar comando → comprobar respuesta → iterar
```

Hoy puede flashear (`deploy.py`), pero no puede **observar**: el log solo sale por WebSocket, `deploy.py` pregunta por `input()`, no hay salida JSON ni exit codes por causa.

**No-objetivos (esta etapa):** servidor MCP, CI, erase desde el CLI, scripts de test (`espbench test x.yaml`), varias Pi en un mismo comando, build desde el CLI (el agente corre `idf.py` él mismo), modo follow (`-f`), endpoint global de eventos.

## 2. Decisiones

| # | Decisión |
|---|---|
| D1 | Usuario: Claude Code en la Mac del dev. |
| D2 | **CLI `espbench` con `--json`** sobre una librería (`client/espbench_lib.py`). MCP después, sobre la misma lib. |
| D3 | Flash por el protocolo TCP existente; todo lo demás por la API HTTP del dashboard. |
| D4 | Placas por `device_key`, SN o MAC (nunca por tty). |
| D5 | `logs` y `wait` unificados: un comando lee un rango `--since … --until …`. |
| D6 | La espera la maneja el cliente (polling). **El `until` lo evalúa solo el server** (una implementación); el cliente lleva loop, timeout, `idle` y `--for`. |
| D7 | Timestamp absoluto por línea, inyectado por `DeviceLog`. |
| D8 | Registro de eventos **por placa** con fecha, hora y cursor. Los anchors se resuelven contra él. |
| D9 | `send` siempre queda como evento. |
| D10 | Anchors con ordinales (`panic~1`) y `--around <evento>`. |
| D11 | `--until` busca hacia adelante desde `--since`: si ya está, rango histórico; si no, espera. |
| D12 | Reserva con vencimiento, usando el mismo par `lock_user`/`lock_token` del flash. |
| D13 | Token opcional en las escrituras de la API. |
| D14 | Un rango **no cruza sesiones** (A1). |

## 3. Log del device

### 3.1 Una sola tubería de líneas

Hoy `DeviceManager.on_serial` escribe el chunk en el log y después alimenta a `SerialWatch`, que tiene su propio decoder y corta también en `\r` (`device.py:253`, `serial_watch.py:91`): las líneas de uno y otro no coinciden y no hay forma de saber el offset de una línea. v2:

```
EspMonitor ─bytes→ DeviceLog.write_serial(bytes)
                     ├ decoder UTF-8 incremental
                     ├ corta en \n  → línea completa
                     ├ escribe "<prefijo><línea>\n" (archivo binario, offset en bytes propio)
                     └ watch.on_line(texto, cursor, ts)   ← SerialWatch sin decoder propio
taglog ──────────→ DeviceLog.write_taglog(level, tag, msg)   (mismo camino, origen "|")
```

- `DeviceLog` abre el archivo **en binario** y lleva el offset sumando bytes codificados (hoy es texto, `device_log.py:123`, y `tell()` no sirve).
- `SerialWatch.on_line(text, cursor, ts)`: recibe la línea ya cortada; aplica él la regla de `\r` (se queda con el último segmento) y el strip de ANSI. Pierde `feed()` y su decoder.

### 3.2 Prefijo

```
2026-10-05 16:02:03.123 > I (120) app_init: App version: v2.4.1
2026-10-05 16:02:05.002 | INFO  | device         | monitoring -> flashing
2026-10-05 16:02:05.100 ↪ ...continuación de la línea serial cortada
```

- `YYYY-MM-DD HH:MM:SS.mmm` + espacio + origen + espacio (26 bytes fijos). Origen: `>` serial, `|` taglog, `↪` continuación serial.
- Taglog dentro del archivo: el cuerpo es `INFO  | tag            | msg` (sin `|` inicial: el del origen ya está). En stdout/tmux sigue con el formato de siempre.
- Mensajes taglog multilínea: cada línea con su prefijo `|`.
- Hora = cuando llega el primer byte de la línea a la Pi.

### 3.3 Línea serial parcial vs taglog

El monitor lee de a 4 KB (`monitor.py:33`) y un prompt de esp_console no termina en `\n`. La línea serial en curso se **retiene en memoria** (no se escribe) hasta `\n` o hasta **150 ms** sin bytes nuevos; ahí se escribe con su `\n`. Si después llega más de esa misma línea, sale con origen `↪`. Así una línea taglog de otro hilo nunca queda pegada a una serial y toda línea del archivo empieza con prefijo.

### 3.4 Sesión y cursor

- **Sesión** = una ejecución del proceso de la placa. `session_id = YYYYMMDD_HHMMSS_<pid>` (pid: evita colisiones si el reloj salta, ver 3.6).
- Primera línea del archivo: header taglog `| INFO  | devicelog | sesión <id> tty=<tty>`. **Se escribe al abrir el archivo, fuera del buffer pre-MAC** (el buffer descarta desde el principio cuando se llena, `device_log.py:69`).
- Rotación: `output.log` → `output_<session_id>.log`, con el id leído del header. Sin header (log viejo) se usa la hora actual, **como hoy** (`device_log.py:163`; no es mtime).
- **Cursor** = `c:<session_id>:<offset>`. `offset` siempre cae **al final de una línea completa**; un `since` que cae a mitad de línea se alinea al inicio de esa línea.
- Archivo de un cursor: sesión actual → `output.log`; si no → `output_<session_id>.log`; si no existe → `cursor_expired`.
- Offsets estables: el marcador `--- adoptado desde … ---` de `_flush_buffer` pasa a escribirse **después** del buffer. Los eventos pre-MAC llevan offset relativo al buffer y se recalculan (+ largo del header) al volcarlo. `_migrate` ya copia al principio.

### 3.5 Compatibilidad

- Líneas sin prefijo (logs viejos): `ts = null`, origen desconocido. Regex: `^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} [>|↪] `.
- **Dashboard en la misma fase** (si no, se rompe): `espbench.js` saca el prefijo antes de `lineClass` (`TAGLOG_RE`/`ESP_LEVEL_RE` están anclados con `^`, `espbench.js:97,105`) y antes de `overwrite` (que corta en el último `\r` y se comería el prefijo, `:154`). Toggle "hora" en la toolbar.

### 3.6 Reloj de la Pi

La Pi no tiene RTC y `devremote.service` no espera a NTP: las primeras sesiones tras un boot arrancan con la hora de fake-hwclock y después NTP salta.
- `devremote.service`: `After=time-sync.target` + `Wants=time-sync.target`, y habilitar `systemd-time-wait-sync`.
- Anchors de tiempo **resueltos en el server**; toda respuesta trae `server_time` con zona horaria. `16:02` se interpreta en la zona de la Pi.
- La búsqueda por tiempo es lineal hacia atrás desde el final (no binaria): tolera saltos y logs sin prefijo.

## 4. Registro de eventos

### 4.1 Archivo y formato

`devices/<MAC>/events.jsonl` (o `devices/unknown-<tty>/events.jsonl` hasta conocer la MAC). Una línea JSON por evento:

```json
{"ts":"2026-10-05T16:02:03.123","type":"panic","cursor":"c:20261005_155000_812:48213",
 "detail":{"kind":"guru","reason":"LoadProhibited","line":"Guru Meditation Error: ..."},"by":"device"}
```

| type | Escribe | detail |
|---|---|---|
| `session` | device (DeviceLog al abrir) | tty, tcp_port, pid |
| `boot` | device (SerialWatch, línea `rst:0x…`, que la ROM imprime siempre) | reason, abnormal |
| `fw` | device (SerialWatch, `app_init`, solo si cambió) | project, version, idf |
| `panic` | device (SerialWatch) | kind, reason, line |
| `boot_loop` | device (SerialWatch) | `start`/`end`, boots |
| `state` | device (FSM) | from, to (incluye `disconnected`) |
| `flash` | device (protocol) | job_id, ok, status, error, user |
| `send` | api | text, enter, user |
| `reserve` / `release` | api | user, expires |

- **`boot` = `rst:`** (antes había `reset` y `boot`; el `rst:` es el **inicio** del arranque, y `app_init` puede no aparecer con log level bajo o si el firmware no cambió).
- **Boot loop**: mientras está activo, los `boot` individuales no se registran (solo `boot_loop start` con el conteo y `boot_loop end`). Si no, un loop escribe miles de eventos por hora.
- Migración unknown→MAC: se migran **solo los eventos de la sesión actual** (el `unknown-<tty>` puede tener sesiones de otra placa).

### 4.2 Escritura

- Device: conoce el cursor exacto (lo lleva `DeviceLog`).
- Api (`send`, `reserve`, `release`): cursor = fin de la última línea completa del `output.log` en ese momento.
- Concurrencia: append `O_APPEND`, **un solo `write()`** por línea (< 4 KB): atómico en Linux para archivos locales. Sin flock.
- **Sin ids y sin rotación** en v1 (con dos escritores, un rename es una carrera; ~200 B por evento, sin boot loop es chico). Un evento se referencia por tipo+ordinal (`panic~1`) o por su cursor.

## 5. Anchors

| Sintaxis | Se resuelve a |
|---|---|
| `now` | fin de la última línea completa |
| `session` | inicio de la sesión actual |
| `boot` `panic` `flash` `send` `state` `fw` | cursor del último evento de ese tipo **en la sesión actual** |
| `boot~N` … | el N-ésimo anterior (`~0` = último) |
| `5m` `30s` `2h` | primera línea con ts ≥ server_time − dur |
| `16:02`, `16:02:03`, `2026-10-05T16:02` | primera línea con ts ≥ esa hora (zona de la Pi) |
| `c:<session>:<offset>` | directo |

- `--since P` → el punto P.
- `--until X` → **el primer X después de `since`**, evaluado en el server. X puede ser un tipo de evento (`boot`, `panic`, `flash`…) o un patrón (`"re:<regex>"`, o cualquier string que no sea un tipo). Si existe → `until_found: true`. Si no → el cliente vuelve a preguntar desde `end`.
- Solo cliente: `idle:<dur>` (sin bytes nuevos por dur), `--for <dur>` (ventana fija, para firmware que loguea seguido y nunca queda idle), `--timeout <dur>`.
- `--around E`: desde el `boot` anterior a E hasta el `boot` siguiente (exclusive), o `--before N` / `--after N` líneas.
- Los patrones matchean el **texto sin prefijo, sin ANSI y después de aplicar `\r`**.
- Un rango no cruza sesiones: si la sesión cambia durante una espera → `session_ended: true` (exit 9).

## 6. Locks y reservas

- Archivo `locks/<tty>`: `user:token[:expires_epoch[:mac]]`. Lectura compatible con `user:token`.
- **`reserve` usa el mismo par `lock_user`/`lock_token` que el flash**, así el flash del propio agente no da `device_locked`. `release` exige el par (como el unlock de hoy, `api.py:170`).
- `LockStore.acquire` **conserva** el vencimiento y la MAC si el lock ya es del mismo user (hoy reescribe `user:token`, `protocol.py:153`).
- Vencido = inexistente en todos lados (`LockStore`, `DeviceRegistry`, api); se borra al leerlo.
- **Reconexión**: `esp32_tmux.sh:44` borra el lock al relanzar la sesión. v2: borra solo locks **sin vencimiento** (los del flash, como hoy). Una reserva vigente se conserva; al arrancar, el proceso de la placa la borra si su MAC no coincide (los ttyUSB se renumeraron). El CLI, en cada escritura, verifica que la reserva siga siendo suya → `reservation_lost` (exit 6).
- **A3 [confirmar]**: una reserva con vencimiento bloquea `send`/`command`/`reset` de otros usuarios (423). Los locks permanentes del flash **no** bloquean `send` (si no, la consola del dashboard muere en toda placa ya flasheada). El dashboard puede forzar con el token de la API.

## 7. API

### 7.1 Direccionamiento

- **Lecturas por placa** (sirven con la placa desconectada, los datos viven por MAC): `/api/board/{key}/…`, donde `key` = device_key, SN o MAC; se resuelve con `devices.json`. La sesión "actual" de una placa desconectada es la última.
- **Escrituras por tty** (necesitan el proceso vivo) con `expect_mac` en el body → `409 device_changed` si no coincide.
- El CLI resuelve con `/api/devices` (ya trae key, SN, MAC, tty, estado).

### 7.2 Endpoints

| Método | Ruta | Qué |
|---|---|---|
| GET | `/api/board/{key}/log?since=&until=&around=&before=&after=&max_lines=&grep=&src=` | Rango del log (7.3) |
| GET | `/api/board/{key}/events?type=&since=&limit=` | Eventos |
| POST | `/api/device/{tty}/reserve` `{lock_user, lock_token, ttl_s, expect_mac}` | 409 si la tiene otro |
| POST | `/api/device/{tty}/release` `{lock_user, lock_token}` | |
| POST | `/api/device/{tty}/send` (existe) | + `expect_mac`; evento `send`; devuelve `cursor` previo |
| GET | `/api/devices`, `/api/device/{tty}` | + `lock_expires` |
| — | `/api/device/{tty}/command/{c}` | validar tty (hoy no lo hace, `api.py:193`) |

Token (D13): archivo opcional `/opt/esp/api_token`. Si existe: escrituras con `Authorization: Bearer <token>`, si no 401. El dashboard pide el token una vez ante un 401 y lo guarda en `localStorage`.

**A2 [confirmar]**: `remote_esp32.py` usa el mismo archivo como token del flash si no viene `--token`. **Consecuencia**: los `.flashcfg.json` sin `token` dejan de poder flashear apenas se cree el archivo. Hoy `esp32_tmux.sh` no pasa `--token`: el flash anda sin auth en producción.

### 7.3 Respuesta de `/log`

```json
{
  "date": "2026-10-05",                      // fecha de la primera línea; las líneas llevan solo la hora
  "lines": ["16:02:03.123 > Guru Meditation Error: ...", "16:02:05.002 | INFO  | device | ..."],
  "start": "c:20261005_155000_812:47100",
  "end":   "c:20261005_155000_812:49380",
  "until_found": true,
  "match": "16:02:03.123 > Guru Meditation Error: ...",
  "partial": null,                           // línea serial en curso (solo si el cliente la pide, para idle)
  "truncated": false,
  "session_ended": false,
  "events": [{"ts":"...","type":"panic","cursor":"..."}],
  "server_time": "2026-10-05T16:05:00.000-03:00"
}
```

- Líneas como strings compactos (ahorra tokens frente a objetos).
- `max_lines` default 200, tope 5000. Si se excede: primeras 50 + últimas `max_lines − 50` + una línea marcador con cuántas se omitieron.
- `src=serial|taglog|all` (default `all`). Las líneas de progreso de esptool (`Writing at 0x… (N %)`) se colapsan en una.
- Sin ANSI salvo `raw=1`.

## 8. Cliente

### 8.1 `client/espbench_lib.py`

Sin `input()`, sin `rich`, sin `print` (callback de log). HTTP con `urllib` (sin dependencias nuevas).
- `Config`: flags > env (`ESPBENCH_HOST`, `ESPBENCH_TOKEN`, `ESPBENCH_USER`) > `~/.config/espbench.json` (perfiles: `--profile`) > `.flashcfg.json` del proyecto (`remote.host`, `lock_user`, `lock_token`, `token`).
- `resolve(name)`, `read_range(...)` (loop client-side), `flash(...)`.
- De `deploy.py` pasa a la lib **solo** el camino con `flasher_args.json` del build dir (`collect_artifact` sin `print`, sin modo custom ni GUI) y `flash_one`. `deploy.py` los importa.

### 8.2 Comandos

| Comando | Opciones |
|---|---|
| `espbench ls` | `--all` (incluye sin MAC) |
| `espbench status <dev>` | |
| `espbench events <dev>\|--all` | `--type a,b` `--since A` `--limit N` (`--all` itera las placas desde el cliente) |
| `espbench logs <dev>` | `--since A` `--until X` `--around E` `--before N` `--after N` `--grep re` `--src s` `--max-lines N` `--timeout D` `--for D` (`--until idle:D`) |
| `espbench send <dev> "txt"` | `--no-enter` + `--until/--for/--timeout/--max-lines`; since implícito = cursor previo al envío; **la línea de eco del comando no cuenta para el match** |
| `espbench flash <dev>` | `--build-dir` `--no-encrypt` `--erase` `--verify[=10s]` `--until X` `--timeout D` |
| `espbench reset <dev>` | `--bootloader` `--verify[=D]` `--until X` |
| `espbench reserve <dev>` | `--ttl 30m` |
| `espbench release <dev>` / `who <dev>` | |
| `espbench restart-session <dev>` | |

Comunes: `--json`, `--host`, `--profile`, `--expect-panic` (un panic en la ventana es el resultado buscado: exit 0).

- **`flash`**: el "done" del protocolo hoy sale **dentro** de `monitor_paused`, antes de relanzar el monitor y de `finish_flash` (`protocol.py:433,457`); un `send` inmediato da 409. v2: el server responde después de salir de `monitor_paused`, con el `cursor` del evento `flash`. El CLI igual espera `state == monitoring` antes de verificar.
- **`--verify[=10s]`**: espera el primer `boot` después del flash y además una **ventana de asentamiento** (default 10 s); falla si en la ventana hay otro `boot`, un `panic` o `boot_loop`. Con `--until X` además espera X.

### 8.3 Exit codes

| Código | `error` (JSON) | Causa | ¿Reintentar? |
|---|---|---|---|
| 0 | — | ok | |
| 1 | `unexpected` | error interno | |
| 2 | `flash_failed` | esptool falló | según `error_hint` |
| 3 | `crashed` | panic, boot loop o reinicio en la ventana | |
| 4 | `timeout` | no apareció `until` | |
| 5 | `busy` | flasheando/borrando | sí, en segundos |
| 6 | `locked` / `reservation_lost` | lock o reserva de otro | no |
| 7 | `not_found` | placa inexistente o desconectada para escritura | |
| 8 | `bad_anchor` / `cursor_expired` | anchor o cursor inválido | |
| 9 | `session_ended` | la placa se desconectó o el proceso se relanzó durante la espera | |
| 10 | `network` / `auth` | sin conexión o 401 | |

El string `error` del JSON es el contrato; el exit code es el resumen.

### 8.4 Skill para agentes

`client/agent/SKILL.md`: cuándo usar `espbench`; siempre `--json`; ciclo (`reserve` → `flash --verify` → `send --until` → `events`/`logs --around` → `release`); qué hacer con cada `error`; cuidar el contexto (`--max-lines`, `--grep`, `--src serial`). Más una sección en el README.

## 9. Fases

1. **Log + eventos (server, device)**: tubería única de líneas, prefijo, retención de parcial, sesión/cursor, header, rotación por session_id, registro de eventos, SerialWatch por líneas, boot=`rst:`, boot loop agrupado. **+ dashboard JS** (prefijo). + `devremote.service` con time-sync.
2. **API**: `/api/board/{key}/log|events`, reservas, `expect_mac`, token, "done" del flash fuera de `monitor_paused`, validar tty en `/command`.
3. **Cliente**: lib + CLI + skill.
4. **Dashboard**: pestaña eventos, vencimiento del lock.
5. **Pi**: offsets reales con esp_idf_monitor, concurrencia de eventos device+api, `send --until` con eco de esp_console, reserva que sobrevive un replug, reloj tras reboot sin red.

### 9.1 Tests

- `DeviceLog`: prefijo con chunks y UTF-8 partidos, `\r`, parcial retenido y continuación `↪`, taglog en medio de una serial parcial, header fuera del buffer, offsets en bytes, rotación por session_id, offsets estables con buffer pre-MAC y migración.
- `SerialWatch.on_line`: los tests de hoy adaptados; `boot` en cada `rst:`; boot loop agrupado.
- Eventos: dos procesos escribiendo a la vez (todas las líneas válidas), migración solo de la sesión actual.
- Anchors: ordinales, `--around` con y sin boot siguiente, `--until` histórico vs espera, tiempo con saltos y líneas sin prefijo, cursor a mitad de línea, cursor vencido, sesión cambiada.
- API: `/log`, `/events` por key con placa desconectada, reservas (vencimiento, mismo par que flash, replug), `expect_mac`, token.
- Protocol: el "done" llega con el monitor relanzado y la FSM en `monitoring`.
- Cliente: lib contra un server fake (`http.server` en thread): idle, `--for`, patrón, eco, timeout, panic, `--expect-panic`, session_ended → exit codes. CLI por `subprocess` con `--json`.
- Dashboard JS: prefijo en `lineClass`/`LineBuffer`.

## 10. Cambios v1 → v2 (revisión)

| Hallazgo | Cambio |
|---|---|
| Offsets de eventos serial imposibles (watch después del chunk, `\r` distinto, archivo en texto) | Tubería única de líneas, archivo binario, `SerialWatch.on_line` |
| Taglog se pega a una línea serial parcial | Parcial retenido hasta `\n`/150 ms, continuación `↪` |
| Evento `boot` no existía (fw solo si cambia) | `boot` = `rst:`; `fw` aparte |
| `--verify = --until boot` no ve un panic a los 2 s | Ventana de asentamiento |
| Reloj de la Pi sin RTC/NTP al boot | time-sync, pid en session_id, tiempo resuelto en server con zona |
| reserve/release incompatibles con el lock del flash y con el replug | Mismo par user/token, `acquire` conserva vencimiento, MAC en el lock, `reservation_lost` |
| Datos por MAC pero endpoints por tty | Lecturas `/api/board/{key}`, escrituras con `expect_mac` |
| "done" del flash antes de relanzar el monitor | Responder fuera de `monitor_paused` |
| Cursores a mitad de línea, eco de esp_console | Cursores en fin de línea, eco excluido del match |
| Header de sesión descartable, rotación "por mtime" falsa, migración de eventos ajenos | Header fuera del buffer, rotación por session_id, migrar solo la sesión actual |
| Exit codes ambiguos | Tabla nueva, `--expect-panic`, `error` como contrato |
| `idle` no sirve con logs periódicos | `--for <dur>` |
| La fase 1 rompía el dashboard | JS del prefijo en la fase 1 |
| `\| \|` doble, boot loop inunda eventos, formato caro en tokens, `/command` sin validar tty | Corregidos |
| Simplificaciones | Sin ids ni rotación de eventos, `until` solo en server, sin `disconnect` (es `state`), sin endpoint global, sin `-f`, sin `--build` |

## 11. Abiertos

- **A2** — token del flash = token de la API (rompe `.flashcfg` sin `token`).
- **A3** — reservas con vencimiento bloquean `send`/`reset` ajenos (423); locks del flash no.
- **A4** — una sola Pi por comando; perfiles en `~/.config/espbench.json`. Varias Pi (`name@host`) después.

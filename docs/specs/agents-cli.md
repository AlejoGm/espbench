# Spec — espbench para agentes (CLI `espbench`, eventos, log con timestamps)

Estado: **v2, revisada y confirmada** · rama `feat/agents` (sobre `feat/dashboard-ux`) · 2026-10-05

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

- `YYYY-MM-DD HH:MM:SS.mmm` + espacio + origen + espacio: **26 caracteres** (28 bytes con `↪`, que ocupa 3 en UTF-8). Origen: `>` serial, `|` taglog, `↪` continuación serial. **Parsear siempre con la regex de 3.5, nunca cortando por bytes.**
- Taglog dentro del archivo: el cuerpo es `INFO  | tag            | msg` (sin `|` inicial: el del origen ya está). En stdout/tmux sigue con el formato de siempre.
- Mensajes taglog multilínea: cada línea con su prefijo `|`.
- Hora = cuando llega el primer byte de la línea a la Pi.

### 3.3 Línea serial parcial vs taglog

El monitor lee de a 4 KB (`monitor.py:33`) y un prompt de esp_console no termina en `\n`. La línea serial en curso se **retiene en memoria** (no se escribe) hasta `\n`, hasta **150 ms** sin bytes nuevos o hasta **1 s (MAX_HOLD) desde su primer byte** aunque sigan llegando (una línea que gotea, `Connecting.....`); ahí se escribe con su `\n`. Si después llega más de esa misma línea, sale con origen `↪`. Así una línea taglog de otro hilo nunca queda pegada a una serial y toda línea del archivo empieza con prefijo.

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
- La búsqueda por tiempo es lineal hacia atrás desde el final (no binaria): tolera saltos y logs sin prefijo. Las horas del archivo **no son monótonas**: la de una línea serial es la de su primer byte, pero se escribe al completarse (hasta MAX_HOLD después), así que una línea taglog posterior puede quedar antes. Algoritmo para "primera línea con ts ≥ T": escanear hacia atrás hasta una línea con ts < T − 2 s (SLACK, > MAX_HOLD) o el inicio de la sesión, y desde ahí avanzar hasta la primera con ts ≥ T.

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
| `fw` | device (SerialWatch, `app_init`): el primero visto en cada sesión + cada vez que cambia | project, version, idf |
| `panic` | device (SerialWatch) | kind, reason, line |
| `boot_loop` | device (SerialWatch) | phase `start`/`end`, boots; el `end` lleva ts = último boot + ventana y `last_boot` {ts, cursor} |
| `state` | device (FSM) | from, to (incluye `disconnected`) |
| `flash` | device (protocol) | job_id, ok, status, error, user |
| `send` | api | text, enter, user |
| `reserve` / `release` | api | user, expires |

- **`boot` = `rst:`** (antes había `reset` y `boot`; el `rst:` es el **inicio** del arranque, y `app_init` puede no aparecer con log level bajo o si el firmware no cambió).
- **Boot loop**: mientras está activo, los `boot` individuales no se registran (solo `boot_loop start` con el conteo y `boot_loop end`). Si no, un loop escribe miles de eventos por hora.
- Migración unknown→MAC: se migran **solo los eventos de la sesión actual** (el `unknown-<tty>` puede tener sesiones de otra placa).

### 4.2 Escritura

- Device: conoce el cursor exacto (lo lleva `DeviceLog`).
- Api (`send`, `reserve`, `release`): cursor = fin de la última línea completa del `output.log` en ese momento (`events.record` → `log_end_cursor`, que lee header y cola con un solo fd: si el log rota en el medio, sesión y offset son del mismo archivo).
- `panic.kind`: `guru`, `abort`, `brownout`, `task_wdt`, `stack_overflow`, `assert`.
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

- **Ordinales** (`panic~1`): sobre los eventos de ese tipo ordenados por (sesión, offset del cursor), **no** por orden en el archivo: device y api escriben en paralelo y un evento puede quedar escrito después de otro con cursor anterior.
- `--since P` → el punto P.
- `--until X` → **el primer X después de `since`**, evaluado en el server. X puede ser un tipo de evento (`boot`, `panic`, `flash`…) o un patrón (`"re:<regex>"`, o cualquier string que no sea un tipo). Si existe → `until_found: true`. Si no → el cliente vuelve a preguntar desde `end`.
- Solo cliente: `idle:<dur>` (sin bytes nuevos por dur), `--for <dur>` (ventana fija, para firmware que loguea seguido y nunca queda idle), `--timeout <dur>`.
- `--around E`: desde el `boot` anterior a E hasta el `boot` siguiente (exclusive), o `--before N` / `--after N` líneas.
- Los patrones matchean el **texto sin prefijo, sin ANSI y después de aplicar `\r`**, sobre la **línea lógica**: una línea `>` más sus `↪` siguientes se unen antes de matchear (un prompt partido por el timeout o MAX_HOLD sigue siendo una línea).
- Un rango no cruza sesiones: si la sesión cambia durante una espera → `session_ended: true` (exit 9).

## 6. Locks y reservas

- Archivo `locks/<tty>`: `user:token[:expires_epoch[:mac]]`. Lectura compatible con `user:token`.
- **`reserve` usa el mismo par `lock_user`/`lock_token` que el flash**, así el flash del propio agente no da `device_locked`. `release` exige el par (como el unlock de hoy, `api.py:170`).
- `LockStore.acquire` **conserva** el vencimiento y la MAC si el lock ya es del mismo user (hoy reescribe `user:token`, `protocol.py:153`).
- Vencido = inexistente en todos lados (`LockStore`, `DeviceRegistry`, api); se borra al leerlo.
- **Reconexión**: `esp32_tmux.sh:44` borra el lock al relanzar la sesión. v2: borra solo locks **sin vencimiento** (los del flash, como hoy). Una reserva vigente se conserva; al arrancar, el proceso de la placa la borra si su MAC no coincide (los ttyUSB se renumeraron). El CLI, en cada escritura, verifica que la reserva siga siendo suya → `reservation_lost` (exit 6).
- **A3 (confirmado)**: una reserva con vencimiento bloquea `send`/`command`/`reset` de otros usuarios (423). Los locks permanentes del flash **no** bloquean `send` (si no, la consola del dashboard muere en toda placa ya flasheada). El dashboard puede forzar con el token de la API.

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

**A2 (confirmado)**: `remote_esp32.py` usa el mismo archivo como token del flash si no viene `--token`. **Consecuencia**: los `.flashcfg.json` sin `token` dejan de poder flashear apenas se cree el archivo. Hoy `esp32_tmux.sh` no pasa `--token`: el flash anda sin auth en producción.

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

- **`flash`**: el "done" del protocolo hoy sale **dentro** de `monitor_paused`, antes de relanzar el monitor y de `finish_flash` (`protocol.py:433,457`); un `send` inmediato da 409. v2: el server responde después de salir de `monitor_paused`, con el `cursor` del evento `flash`. El CLI igual espera `state == monitoring` antes de verificar. Al moverlo, **`record_flash_event` tiene que seguir antes de reanudar el monitor**: si no, el primer `boot` del firmware nuevo puede quedar con un cursor anterior al del `flash` y `--verify` (que busca el boot *después* del flash) no lo ve.
- **`--verify[=10s]`**: espera el primer `boot` después del flash y además una **ventana de asentamiento** (default 10 s); falla si en la ventana hay otro `boot`, un `panic` o `boot_loop`. Con `--until X` además espera X.

### 8.3 Exit codes

| Código | `error` (JSON) | Causa | ¿Reintentar? |
|---|---|---|---|
| 0 | — | ok | |
| 1 | `unexpected` / `bad_request` | error interno, o pedido inválido (regex, parámetros) | |
| 2 | `flash_failed` | esptool falló | según `error_hint` |
| 3 | `crashed` | panic, boot loop o reinicio en la ventana | |
| 4 | `timeout` | no apareció `until` | |
| 5 | `busy` | flasheando/borrando, o placa todavía sin MAC (409) | sí, en segundos |
| 6 | `locked` / `reservation_lost` / `token_mismatch` | lock o reserva de otro (423/409); la reserva ya no es tuya (423); par user/token incorrecto (403) | no |
| 7 | `not_found` / `device_changed` / `session_down` | placa inexistente o desconectada para escritura; en el tty hay otra placa (`expect_mac`, 409); la sesión tmux del device no está (502) | no (resolver de nuevo con `/api/devices`; `session_down`: `restart-session`) |
| 8 | `bad_anchor` / `cursor_expired` | anchor o cursor inválido | |
| 9 | `session_ended` | la placa se desconectó o el proceso se relanzó durante la espera | |
| 10 | `network` / `auth` / `auth_config` | sin conexión, 401, o el token de la Pi ilegible (500) | |

El string `error` del JSON es el contrato; el exit code es el resumen. Hay varios 409 (`busy`, `device_changed`, `locked`) y varios 423 (`locked`, `reservation_lost`): el cliente los distingue **por el string `error`**, nunca por el status HTTP. Los errores llegan como `{"detail": {"error", "message"}}`.

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

Ninguno. A1–A4 confirmados por Alejo (2026-10-05):
- **A1** — un rango no cruza sesiones (D14).
- **A2** — token del flash = token de la API (`/opt/esp/api_token`). Avisar en el README: los `.flashcfg` sin `token` dejan de flashear cuando se crea el archivo.
- **A3** — reservas con vencimiento bloquean `send`/`reset` ajenos (423); locks del flash no.
- **A4** — una sola Pi por comando; perfiles en `~/.config/espbench.json`.

## 12. Fase 2 (API): decisiones de implementación

Donde la spec no alcanzaba (implementado en `remote/server/logrange.py`, `locks.py`, `auth.py`, `api.py`):

- **Anchors de evento solo en la sesión actual**, ordinales incluidos (`boot~3` no salta a una sesión anterior). A una sesión anterior se llega con un cursor sacado de `/events`.
- **`until boot|panic` se busca en las líneas** (`serial_watch.line_kind`, la misma detección de `SerialWatch`), no en `events.jsonl`: el evento se escribe un instante después de su línea, y un poll que viera la línea sin el evento seguiría desde un `end` posterior y no lo encontraría nunca. Los demás tipos salen de `events.jsonl`, leído después de fijar el tamaño del log.
- `--since T --until T` (mismo tipo): el siguiente, no el mismo.
- El `until` no depende de `src` ni de `grep` (definen qué se muestra, no el rango).
- **`echo=<texto>`** en `/log` (para `send --until`): la primera línea lógica que termina con ese texto no cuenta para el match. Es como el server excluye el eco (§8.2), ya que el `until` lo evalúa solo él.
- `--around`: los bordes son las líneas `rst:` (también en un boot loop, donde no hay eventos `boot` sueltos).
- **Línea lógica partida entre polls**: el `until` arranca sembrado con la línea lógica abierta en `since` (el `>` anterior y sus `↪`). Si lo de antes de `since` ya matcheaba o era el eco, no vuelve a contar; si no, un `↪` que llega en el poll siguiente (o la respuesta pegada al prompt previo al send) completa la línea y matchea. `match` es la línea lógica entera.
- **`echo_seen`** en la respuesta: cursor de la línea lógica que se tomó como eco (o `null`). El eco no tiene estado en el server.
- `grep` y `until=re:`: hasta 256 caracteres, primeros 4096 de cada línea, timeout con el módulo `regex` (sin él, se rechazan los cuantificadores anidados y las alternancias cuantificadas) → `bad_request`.
- `/events`: `limit` = los **últimos** N (en orden cronológico), `more: true` si quedaron afuera; `order=asc` = los primeros N desde `since` (inclusive: para paginar, descartar los ya vistos).
- `partial` es siempre `null`: la línea en curso vive en la memoria del proceso del device; sale al archivo a los 150 ms / 1 s.
- `since` default = `session`; `until_found` = `null` si no se pidió `until`.
- Una hora sin fecha (`23:50`) posterior a ahora (más de 1 min) es de ayer.
- `max_lines` < 100: la cabeza es `max_lines // 2`. Una línea de otra fecha que `date` lleva la fecha completa.
- `events` de la respuesta incluye `detail` (motivo del boot, tipo de panic).
- `/events?since=<tiempo>` compara la hora del evento (cruza sesiones); con otro anchor, (sesión, offset).
- **Errores**: `{"detail": {"error", "message"}}` en las escrituras y en `/api/board`. `bad_anchor`/`bad_request` 400, `cursor_expired` 410, `not_found` 404, `device_changed` 409, `busy` 409, `locked` (423 en `send`/`command`, 409 en `reserve`), `token_mismatch` 403, `auth` 401, `session_down` 502 (tmux sin la sesión del device en `send`/`command`).
- **Reserva**: `ttl_s` default 1800, máximo 7 días; reservar de nuevo renueva; exige que la placa tenga MAC (si no, 409 `busy`); contra un lock permanente ajeno el mensaje sugiere `unlock`. `release` suelta cualquier lock del par (también el del flash), como `unlock`. `user` y `token` no pueden tener `:`. Un lock vencido se ignora (no se borra al leerlo) y todo leer-decidir-escribir va bajo `flock` (`locks/<tty>.lck`).
- **`force: true`** (solo el booleano) en `send`/`command` saltea una reserva ajena: lo manda el dashboard después de confirmar; **el CLI nunca lo manda**. Queda `forced: true` y el `user` en el evento. `reserve` no tiene `force`.
- **`require_reservation: true`** en `send`/`command`: la escritura sale solo si el par tiene la reserva vigente, chequeado en el mismo pedido → 423 `reservation_lost`. Es lo que implementa `reservation_lost` (el server no lo detecta solo).
- `devremote-reset` (`espbench restart-session`) sigue las mismas reglas de reserva que `send`/`command` (423 `locked` ante una reserva ajena, `force` del dashboard, `require_reservation` y `expect_mac` del CLI): mata el proceso de la placa.
- `reservation_lost` dice por qué: venció o la soltaron (sin lock: volver a reservar), la tiene otro (hasta cuándo), mismo user con otro token, o hay un lock de flash sin vencimiento.
- `/command` registra un evento **`command`** (tipo nuevo: command, user, forced?) y devuelve su `cursor`, tomado **antes** de mandar las teclas (como `send`: el `rst:` de un reset sale en milisegundos y si no quedaba antes del cursor); 409 `busy` si flashea/borra, 502 si tmux falla.
- **Token**: se lee en cada pedido (API) y en cada conexión (flash): crearlo no requiere reiniciar nada.
- El evento `send` solo se registra si tmux lo mandó; su cursor se toma antes de mandar.

### 12.1 Notas para el cliente (fase 3)

- **Pollear siempre desde `end`** de la respuesta anterior, y mandar `echo` hasta que llegue `echo_seen` (después, sin `echo`).
- `session_ended` es terminal **solo si `until_found` es falso**: si el `until` apareció antes de que la sesión terminara, el resultado vale.
- **Placas USB-Serial-JTAG (S3/C3)**: el reset después del flash re-enumera el USB y arranca una sesión nueva del proceso. `--verify` tiene que seguir en la sesión nueva (`since=session` de la nueva, esperando `state == monitoring`) en vez de salir con exit 9.
- Las escrituras de un agente reservado van con `require_reservation: true` (→ `reservation_lost`, exit 6) y `expect_mac` (→ `device_changed`).
- Cada línea de panic es un evento: un `assert failed` seguido de `abort() was called` son 2 `panic`.
- El texto de los `send` queda en `events.jsonl` y las lecturas no piden token: cualquiera en la red lo ve. No mandar secretos por la consola.

## 12.2 Fase 3 (cliente): decisiones de implementación

Implementado en `client/espbench_lib.py`, `client/espbench.py`, `client/agent/SKILL.md`. Tests contra `tests/benchsim.py` (Pi simulada: API real + `DeviceManager`/`DeviceLog` reales, tmux y esptool falsos).

**Contrato real vs spec** (manda el server):
- El flash (TCP) no habla el contrato de la API: `_FLASH_ERRORS` traduce `unauthorized`→`auth`, `auth_config`, `device_locked`→`locked`, `token_mismatch`, `device_busy`→`busy`, `device_changed`, `lock_credentials_required`→`bad_request`; esptool con rc≠0 y `exception`/`esptool_not_found`/`flash_critical_error` → `flash_failed` (con `error_hint` y `log_tail`); socket caído → `network`.
- Respuestas sin `{"detail": {"error"}}` (`/api/device/{tty}` viejo, validación 422 de FastAPI) se traducen por status: 400/422 `bad_request`, 401 `auth`, 403 `token_mismatch`, 404 `not_found`, 409 `busy`, 410 `cursor_expired`, 423 `locked`; el resto `unexpected`.
- **Fix en el server** (v0.22.12): el `cursor` de `/command` se tomaba después de mandar las teclas; el `rst:` de un reset sale en milisegundos y quedaba antes, así que `reset --verify` no lo veía. Ahora se toma antes, como en `send`.
- `ESPBENCH_LOCK_TOKEN` se agrega a la precedencia de §8.1 (faltaba el token del par), más `ESPBENCH_PROFILE`, `ESPBENCH_CONFIG` (ruta del archivo de perfiles) y `ESPBENCH_STATE_DIR`.

**Decisiones**:
- **`require_reservation`**: el CLI necesita saber si "está reservado". `reserve` guarda un registro local (`~/.cache/espbench/reservations.json`, **por MAC**: el host se escribe de muchas formas; con `flock` y reemplazo atómico) y `release` lo borra, igual que un `reservation_lost` (el error dice que se olvidó y cómo volver a reservar). Dos agentes con la misma config comparten la reserva: un `ESPBENCH_USER` por agente; mientras existe, `send`/`command` van con `require_reservation: true`. Así una reserva vencida o soltada por otro da `reservation_lost` en la próxima escritura. El flash (TCP, sin ese campo) lo chequea antes contra `/api/devices` (`lock_user` propio y `lock_expires`).
- **Crash durante una espera**: sale de `/events`, no de los `events` de cada `/log`. Encontrado con la placa simulada: el cursor de un evento es el inicio de su **línea lógica**, y un panic que llega como `↪` del prompt `esp> ` (que ya salió solo a los 150 ms) queda con el cursor del prompt: antes del `start` del poll que lo trae, y a veces antes del inicio del rango. Regla: cuenta un crash con cursor en `[start, end)`, o un **`panic` nuevo** (no estaba en la foto) de la misma sesión con cursor < `end`. La regla de "nuevo" es **solo para `panic`**: `boot`/`boot_loop` empiezan en su propia línea `rst:`, y con ella el boot que `--verify` acababa de encontrar por su línea (su evento llega después) se tomaba como reinicio en la ventana. La **foto se toma antes de escribir** (`crash_snapshot()` antes de `send`/`flash`/`reset`); si no se pudo (error que no es de red), no se aplica la regla de "nuevo" (mejor perder el caso del prompt que contar panics viejos). Se consulta en los polls con actividad (este o el anterior) y una vez al cerrar (evento escrito después del último poll).
- Un crash en el rango corta la espera con `crashed` aunque el `until` aparezca más adelante en el mismo poll; con `until=panic|boot_loop` no es crash. `--expect-panic` vale solo para `panic` (un `boot_loop` o el reboot de la ventana siguen siendo `crashed`).
- **`idle:D`** = sin líneas nuevas por D. En `send` el **eco no es la respuesta**: el cliente manda `echo` también con idle (con un `until` que nunca matchea, `re:(?!)`, para que el server evalúe las líneas lógicas y devuelva `echo_seen`) y el silencio cuenta recién desde la primera línea nueva **después** del eco. Si en D no apareció el eco (firmware sin eco), deja de esperarlo y cuenta desde la primera línea nueva. En `logs`, desde el primer poll. Sin `--timeout`, 30 s.
- **Argumentos antes de escribir**: duraciones (`--timeout`, `--for`, `--verify`, `--ttl`, `idle:`) y regex (`--until re:`, `--grep`, compiladas con `re`) se validan antes de `send`/`flash`/`reset`/`reserve`: un `bad_request` nunca llega después de una escritura. Un error posterior a la escritura (red, timeout de la Pi) lleva lo que ya salió: `sent`, `cursor`, `job_id`, `status`.
- **JSON compacto**: `until_found`/`match` solo si hubo `until`; `truncated`/`session_ended` solo si son true; sin `server_time`; sin los eventos del api en el punto de partida (`send`, `reserve`... del mismo cursor) ni anteriores al rango; en `send`, sin `start` (es el `cursor`); `verify` ok sin `lines`/`events` salvo `--max-lines`. Los eventos se deduplican por su contenido entero (dos `state` comparten cursor).
- **`--for D`**: solo, ventana fija que termina ok; con `--until`, es el tope (no encontrado = `timeout`).
- Las líneas se acumulan entre polls con el mismo cabeza + cola de §7.3 (`max_lines`); una línea de un poll con otra `date` lleva la fecha.
- **`--verify[=D]`**: (1) primer `boot` desde el cursor del flash/command; si llega `session_ended` sin boot, espera a que `/events` informe otra `session` y sigue con `since=c:<nueva>:0` (S3/C3); (2) ventana D con `fail_on = panic, boot_loop, boot` (`boot` → mensaje "se reinició en la ventana"); `session_ended` en la ventana → exit 9 (no se distingue un replug de un reset); (3) con `--until X`, X desde el boot. Timeout del boot y del until: `--timeout` (default 60 s). Si el server no devuelve cursor, se usa un `now` tomado antes de escribir. `flash --until X` sin `--verify` = verify con ventana 0. Después del flash, antes de verificar, espera `state == monitoring` por MAC (hasta 10 s; si no llega, verifica igual).
- `reset --bootloader` no se combina con `--verify`/`--until` (`bad_request`): en download mode no hay boot que esperar.
- `resolve`: una placa que no está en `/api/devices` (desconectada) se lee igual por el nombre (la Pi la resuelve en `/api/board/{key}`); escribir exige que esté viva → `not_found`. La clave de `/api/board` es la MAC sin separadores. `ls` sin `--all` oculta las placas sin MAC.
- hw_model del build distinto del de la placa: `warnings` en la respuesta, no bloquea (deploy preguntaba y/N; sin TTY seguía).
- Comunes antes o después del subcomando. Sin `--token` (va por env/perfil/`.flashcfg.json`). `restart-session` = `POST /devremote-reset` (pide token; reserva como `command`). `events --all` recorre las placas de `/api/devices`.
- De `deploy.py` pasan a la lib `collect_artifact` (solo el camino del build dir), `flash_one` y `hw_model_from_build`; deploy envuelve los dos primeros con sus prints (salida idéntica) y conserva el modo custom.
- Python del sistema en la Mac del dev: tiene uvicorn 0.39 (el test con uvicorn corre en los dos intérpretes).

**Para la Pi** (fase 5): eco real de `esp_console` en `send --until`; `flash --verify` en una S3/C3 (tiempo de re-enumeración, tty que cambia, que el boot caiga en la sesión nueva); un panic real con el backtrace decodificado de `esp_idf_monitor`; carga de los polls (hasta 2 pedidos por poll con actividad) con varios agentes.

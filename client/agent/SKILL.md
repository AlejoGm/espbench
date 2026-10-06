---
name: espbench
description: Flashear y observar placas ESP32 reales en la Pi de espbench con el CLI `espbench`. Usala para flashear un build en una placa remota, verificar que arranca, mandar comandos por la consola serie y esperar la respuesta, leer el log serie o los eventos (boot, panic, flash) de una placa, o reservar una placa del banco.
---

# espbench

`espbench` maneja placas ESP32 enchufadas a una Raspberry Pi (el banco): flash, consola serie, log con hora por línea y eventos por placa. Corre en esta máquina. Sin host configurado (`ESPBENCH_HOST` / `~/.config/espbench.json` / `remote.host` del `.flashcfg.json`) encuentra solo los benches de la tailnet: `espbench benches --json` los lista, `ls` muestra las placas de todos (campo `bench`) y cada comando busca la placa en todos. `espbench <cmd> --help` tiene todas las opciones.

## Configuración (una vez, antes de reservar)

`ESPBENCH_USER` y `ESPBENCH_LOCK_TOKEN` son tu identidad en el banco: **uno por agente**. Dos agentes con el mismo par comparten la reserva y se pisan sin enterarse. Si el entorno no los tiene, pedíselos al usuario (o usá `<usuario>-<tarea>` con un token propio). `espbench who <dev> --json` muestra quién tiene la placa (`mine: true` = vos).

## Reglas

- **Siempre `--json`.** Cada comando imprime un solo objeto JSON. Decidí por el **exit code** y el string `error`, nunca parseando texto.
- La placa se nombra por `device_key`, SN o MAC (`espbench ls` las lista). El tty puede cambiar entre comandos. Si está en dos benches (`ambiguous`, exit 7, `matches` dice dónde): `<dev>@<bench>` o `--bench <bench>`.
- El build lo hacés vos (`idf.py build`); `espbench flash` sube lo que hay en `--build-dir` (default `build`).
- El texto de cada `send` queda en el log de eventos de la Pi y cualquiera en la red lo lee: mandá comandos, nunca secretos.

## Ciclo

1. `espbench ls --json` → elegí una placa con **`available: true`**: en `monitoring` y con `lock_user: null` (libre) o con tu usuario. Una placa con `lock_user` de otro no te sirve aunque no tenga `lock_expires`: es el lock que dejó su último flash, y te va a dar `locked` al reservar o flashear (ver Errores).
2. `espbench reserve <dev> --ttl 30m --json` → nadie más le escribe mientras trabajás. Desde acá tus escrituras exigen que la reserva siga siendo tuya.
3. `idf.py build`, después `espbench flash <dev> --verify --json` → flash + espera el primer boot + 10 s de asentamiento sin reboot ni panic. Exit 0 = el firmware nuevo arrancó y se quedó arriba.
4. `espbench send <dev> "<comando>" --until "<texto esperado>" --json` → `match` trae la línea que lo cumplió; `lines`, lo que salió desde el envío.
5. Si algo crasheó: `espbench events <dev> --type panic --json`, después `espbench logs <dev> --around panic --json` (del boot anterior al siguiente).
6. Iterá 3–5. Al terminar: `espbench release <dev> --json`.

## Esperas

`--until X` busca hacia adelante y corta en el primero; si no aparece, espera (`--timeout`, default 30 s).

| `--until` | Corta cuando |
|---|---|
| `boot`, `panic`, `flash`, `send`, `fw`, `state` | aparece ese evento |
| `"re:<regex>"` | una línea matchea la regex |
| `"<texto>"` | una línea contiene el texto |
| `idle:500ms` | pasa ese tiempo sin líneas nuevas (en `send`, contado desde la primera línea de respuesta) |

- `--for 5s`: ventana fija, para firmware que loguea seguido y nunca queda idle.
- En `send` el eco del comando nunca cuenta para el match: `--until status` sobre `send status` espera la respuesta.
- Un panic o boot loop durante la espera la corta con exit 3. Si el panic es lo que estás probando: `--expect-panic` (exit 0, `reason: "panic"`).
- `--verify=D` cambia la ventana de asentamiento; `flash`/`reset` con `--until X` además esperan X después del boot.
- `verify.boot_loop: true` con exit 0: el boot que encontró es el que la Pi marcó como inicio de un boot loop (varios resets seguidos que no pasaron por `espbench reset`, p. ej. el botón EN). Es informativo: si el firmware de verdad reinicia en loop, la ventana lo ve y da exit 3.
- `idle:D` en `send` cuenta desde la primera línea **después del eco**: elegí D mayor que la pausa más larga entre líneas de la respuesta.

## Anchors (`--since`, `--around`)

`now`, `session` (default), un evento con ordinal (`boot` = el último de la sesión, `boot~1` = el anterior, `panic`, `flash`, `send`), tiempo (`500ms`, `30s`, `5m`, `16:02`, `2026-10-05T16:02`, hora de la Pi) o un cursor `c:<sesión>:<offset>` de una respuesta anterior (`start`, `end`, `cursor`, `boot_cursor`, `match_cursor` = inicio de la línea del `match`). Un rango no cruza sesiones.

Durante un boot loop los panics no quedan como eventos sueltos: mirá el `boot_loop` (`detail.panics`, `first_panic`, `last_panic`) y `logs --around <su cursor>`.

Los anchors de evento (`panic`, `boot~1`) buscan **solo en la sesión actual**. Después de un cambio de sesión (replug, `restart-session`, S3/C3 tras el flash) `--around panic` da `bad_anchor` aunque el panic exista: tomá su cursor de `events`, que cruza sesiones.

```
$ espbench events mi-board --type panic --limit 1 --json
{"ok": true, "events": [{"type": "panic", "cursor": "c:20261006_015217_10574001:760", ...}], ...}
$ espbench logs mi-board --around c:20261006_015217_10574001:760 --max-lines 80 --json
```

## Cuidar el contexto

Los logs se comen tokens. Pedí lo justo:

- **`logs` siempre con `--since`** (`boot`, `flash`, `5m`, o el `end`/`cursor` de la respuesta anterior). Sin `--since` lee la sesión entera: en una placa que corre hace días es todo el log desde que arrancó el proceso.
- `--max-lines N` (default 200; con más, cabeza + cola y la línea `… N líneas omitidas …`).
- `--grep '<regex>'` para ver solo lo que importa (no afecta el `until`).
- `--src serial` saca las líneas del server (flash, transiciones).
- `events` antes que `logs`: un panic es un evento con `detail.reason`, sin bajar el log.

## Errores

| exit | `error` | Qué hacer |
|---|---|---|
| 0 | — | ok |
| 1 | `bad_request`, `unexpected` | corregí el pedido (regex, anchor de tiempo, opción); `unexpected`: reportalo |
| 2 | `flash_failed` | leé `error_hint` y `log_tail`; rc 2 suele ser placa que no entra en download mode: `espbench reset <dev> --bootloader` y reintentá una vez |
| 3 | `crashed` | el firmware crasheó: mirá `crash` y `lines`, después `logs --around panic`. Es un bug del firmware, no del banco |
| 4 | `timeout` | no apareció el `until`: mirá `lines` (¿salió otra cosa?) antes de subir el `--timeout` |
| 5 | `busy` | la placa está flasheando o sin MAC todavía: reintentá en unos segundos |
| 6 | `locked`, `reservation_lost`, `token_mismatch` | `locked`: otra persona tiene la placa. `espbench who <dev> --json`: con `reservation: true` es una reserva (vence sola: elegí otra placa o esperá); con `reservation: false` es el **lock permanente de su último flash** (no vence): elegí otra placa con `available: true`, o pedile al usuario que el dueño la suelte (`python client/deploy.py --unlock` con su `.flashcfg.json`, o `devremote --unlock <tty>` en la Pi). Nunca reintentes en loop. `reservation_lost`: tu reserva venció o la soltaron; `message` dice cuál. Si venció y nadie la tomó, `espbench reserve <dev> --json` y reintentá la escritura una vez; si la tiene otro, pará y avisá |
| 7 | `not_found`, `ambiguous`, `device_changed`, `session_down` | la placa no está o en su puerto hay otra: `espbench ls --json` y resolvé de nuevo. `ambiguous`: el mismo nombre en dos benches, elegí con `<dev>@<bench>`. `session_down`: el proceso de la placa en la Pi no corre: `espbench restart-session <dev> --json` y reintentá una vez |
| 8 | `bad_anchor`, `cursor_expired` | el anchor no existe en esta sesión (`panic` sin panics) o el cursor es de una sesión borrada: usá `session` o `5m` |
| 9 | `session_ended` | la placa se desconectó o su proceso se relanzó: `espbench ls --json`; si volvió, seguí desde `--since session` |
| 10 | `network`, `auth`, `auth_config` | sin conexión con la Pi o token de la API faltante/incorrecto (`ESPBENCH_TOKEN`): avisale al usuario |

Salvo el `reserve` de una reserva vencida, con exit 6 o 10 no reintentes en loop: lo resuelve una persona. Si un error llega **después** de escribir, el JSON trae lo que ya salió (`sent`, `cursor`, `job_id`): no repitas la escritura a ciegas, leé desde ese `cursor`.

## Ejemplos (salidas reales, recortadas)

```
$ espbench send sim-board status --until idle:300ms --json
{"ok": true, "sent": "status", "cursor": "c:20261006_015217_10574001:607", "reason": "idle",
 "lines": ["01:52:40.533 ↪ status", "01:52:40.594 > OK uptime=12s heap=210000", "01:52:40.594 > esp> "], ...}

$ espbench send sim-board panic --until OK --timeout 5s --json        # exit 3
{"ok": false, "error": "crashed", "reason": "crash",
 "lines": ["01:52:41.356 ↪ panic", "01:52:41.408 > Guru Meditation Error: Core  1 panic'ed (LoadProhibited). ...", ...],
 "crash": {"type": "panic", "cursor": "c:...:760", "detail": {"kind": "guru", "reason": "LoadProhibited", ...}}}

$ espbench flash sim-board --verify=2s --json
{"ok": true, "status": "exitoso", "cursor": "c:...:1553",
 "verify": {"ok": true, "boot": "01:52:52.332 > rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)",
            "boot_cursor": "c:...:1714", "window_s": 2.0, "end": "c:...:2065", "reason": "for"}}
```

Formato de línea: `HH:MM:SS.mmm <origen> <texto>`; origen `>` serial, `↪` continuación de la línea serial anterior (un prompt sin `\n`, el eco), `|` línea del server. `date` trae la fecha.

# client/

Lo que corre en la máquina del developer (Mac, Python 3.9+: nada de `X | None` en firmas ni anotaciones de módulo).

| Archivo | Para quién | Qué |
|---|---|---|
| `deploy.py` | humanos | build + flash local o remoto, interactivo (`input()`, `rich`), modo custom con file picker |
| `espbench.py` | agentes | CLI `espbench`: `--json` (un objeto por comando), exit codes por causa |
| `espbench_lib.py` | los dos | config, API HTTP (urllib), rangos del log con espera, flash por TCP + verify. Sin `input()`, `rich` ni `print` |
| `agent/SKILL.md` | Claude Code | skill de `espbench` (se instala con un symlink a `~/.claude/skills/espbench`) |
| `install.sh` | | venv (`rich` para deploy) + wrapper `espbench` en `~/.local/bin`. Idempotente. Overrides: `ESPBENCH_VENV`, `ESPBENCH_BIN_DIR`, `ESPBENCH_SKIP_PIP` |

Spec del CLI y de la lib: `docs/specs/agents-cli.md` §8 y §12.2 (decisiones de esta fase). Arquitectura: `docs/ARCHITECTURE.md` §11.

## espbench_lib

- **Contrato**: todo error es `EspbenchError(error, message)`; `error` es el string de §8.3 y define el exit code (`EXIT_CODES`). Los errores del protocolo TCP del flash se traducen en `_FLASH_ERRORS`; un HTTP sin `{"detail": {"error"}}` se traduce por status.
- **Config** (`Config.load`): flags > env (`ESPBENCH_HOST`, `ESPBENCH_TOKEN`, `ESPBENCH_USER`, `ESPBENCH_LOCK_TOKEN`, `ESPBENCH_PROFILE`, `ESPBENCH_CONFIG`) > `~/.config/espbench.json` (perfil, después claves sueltas) > `remote` del `.flashcfg.json` (el del directorio actual o el primero hacia arriba; con lista, la entrada cuyo `name`/`device_key` es la placa pedida).
- **Placas** (`Client.resolve`): por `device_key`, SN, MAC o tty contra `/api/devices`. `Board.key` es la MAC sin separadores (lo que va en `/api/board/{key}`). Una placa que no está en `/api/devices` se puede leer igual (la Pi la resuelve por key); para escribir tiene que estar viva.
- **Escrituras** (`_write_body`, `_write`): siempre `expect_mac` y el par del lock; `require_reservation: true` si este cliente la reservó (registro local por MAC en `~/.cache/espbench/reservations.json`, `ESPBENCH_STATE_DIR`, con `flock`; se olvida ante `reservation_lost`). **Nunca `force`.** El CLI valida todos los argumentos (duraciones, regex) antes de escribir, y toma la foto de crashes (`crash_snapshot`) antes de la escritura.
- **Esperas** (`read_range`): poll cada 0,3 s desde el `end` anterior, `echo` hasta `echo_seen`. Los crash (panic, boot_loop) salen de `/events`, no de los `events` de cada respuesta: ver el docstring de `_crash_since` (cursor = inicio de la línea lógica; evento escrito después de su línea; la regla de "nuevo" solo para `panic`). `idle:` en `send` cuenta desde la primera línea después del eco.
- **Flash**: `collect_artifact` (solo `flasher_args.json` del build dir) + `flash_one` (protocolo TCP) viven acá; `deploy.py` los importa (con sus prints) y mantiene aparte solo el modo custom.
- **Verify**: primer `boot` después del cursor del flash/reset (si la sesión termina, sigue en la nueva: S3/C3), ventana de asentamiento sin reinicio (por la línea `rst:`, `_settle`), `panic` ni `boot_loop`, y `until` opcional. Un `boot_loop` que empieza en el boot encontrado (su cursor = `match_cursor`) es informativo: `boot_loop: true`.
- **`ls`/`status`**: `available` (monitoring y sin lock, o con lock propio, y sin `estado` con `exclude_pick`: `avoid`). Traen `props`, `note`, `note_by`, `note_at`. `Client.summarize` usa los `exclude_pick` del catálogo del bench (`/api/properties`; sin él, `DEFAULT_EXCLUDE`).
- **Nota y propiedades**: `note`, `set` (`parse_set_ops`: `cat=v`, `cat=a,b`, `cat+=v`, `cat-=v`, `cat=`) y `props [add|rm]` → `PATCH /api/devices/{MAC}` / `/api/properties` con `user` = `ESPBENCH_USER`. Se validan contra el catálogo antes de escribir (`check_props`, con el más parecido). `ls --where` / `pick --where` (`parse_where`, `matches_where`, AND; con varios benches, la unión de los catálogos). `pick` (`pick_order`): disponible, con MAC, sin boot loop, no reservada por mí (`--include-mine`), sin nota primero; `--reserve` prueba la siguiente si una ya la tomó otro o el bench rechaza el token (`locked`, `busy`, `auth`, `network`...: `skipped` con `bench`). `note`/`set`/`props` piden antes `/api/properties` (`Client.require_meta`): un bench espbench sin propiedades da `unsupported`. `props add/rm` con varios benches pide `--bench`; `props` sola, la unión (`merge_categories` con `benches` por valor). `<dev>@<bench>` con host fijo: vale si es ese bench (`/api/version`), si no `bad_request`. Tokens por bench: `"tokens"` en `espbench-benches.json` (`config_for_bench`; flag/env ganan). `benches` usa el catálogo de cada bench para `available` y dice `props: true/false`.
- **Discovery** (`Benches`, sin host / `host: "auto"` / `--bench`): `ls` y `events --all` de todos los benches; `<dev>`
  se resuelve con `benches.resolve` y el CLI sigue contra ese bench con la MAC (`out.extra["bench"]`). Cache de la
  lista de benches en `ESPBENCH_STATE_DIR/benches.json` (30 s); si la placa no está, un scan nuevo. Ver ARCHITECTURE §11.
- **Windows**: `fcntl` es opcional (import con fallback): sin él, el registro local de reservas va sin `flock`. `deploy.py` necesita `client/espbench_lib.py` y `common.py` al lado (no se copia suelto).

## Tests

`tests/test_espbench_lib.py` (lib), `tests/test_espbench_cli.py` (CLI por subprocess), `tests/test_deploy.py`. Corren contra `tests/benchsim.py`: la API real (adaptador `http.server`, o uvicorn) y una placa con `DeviceManager`/`DeviceLog` reales (hold de la línea parcial en 30 ms en vez de 150); tmux y esptool falsos. Los del CLI llaman a `espbench.main(argv)` en el proceso; por subprocess solo el contrato JSON, `python -m` e `install.sh`. Para probar a mano: `ESP_BASE=$(mktemp -d) python -m tests.benchsim --port 8099`.

## `.flashcfg.json` (deploy y espbench)

```json
{
  "mode": "auto",
  "paths": { "project_root": ".", "idf_py": "idf.py" },
  "local":  { "port": "/dev/ttyUSB0", "monitor": true },
  "remote": [{ "name": "mi-board", "host": "sensipi03", "token": "", "lock_user": "yo", "lock_token": "secreto" }],
  "chip": "esp32",
  "flash_baud": 921600,
  "encrypt": true,
  "erase": false
}
```

`deploy.py --build-dir` (default `build`) elige el build dir a compilar y flashear (relativo a `project_root`), y va en cada `idf.py -B`. `espbench flash --build-dir` igual, sin compilar.

## Benches (`benches.py`)

Encuentra los benches (hosts con el dashboard de espbench en :8080, Pi u otra máquina) y en cuál está cada placa.
Solo stdlib: lo usan `deploy.py`, bench-master (`master/`) y el CLI `espbench` sin host (`espbench_lib.Benches`).

- Candidatos: peers **online** de `tailscale status --json` + `hosts` de `~/.config/espbench-benches.json`
  (`ESPBENCH_BENCHES_CONFIG` para otra ruta): `{"tailscale": true, "hosts": ["10.0.0.5", "lab:8080"], "timeout_s": 2}`.
- Es bench si `GET /api/version` devuelve `app: "espbench"` (o solo `{"version"}`, benches sin actualizar: `legacy`,
  el CLI los ignora). Lo identifica la MAC de la máquina (`id`, `Bench.key`; sin ella, el nombre): dos caminos al
  mismo bench cuentan una vez.
- `resolve(key)`: key = device_key, SN, MAC, tty, `<dev>@<bench>` o `<bench>/<tty>`. `ResolveError` (`kind`:
  `not_found` / `ambiguous`, con `hits`) si no está o si está en más de un bench.
- `scan_cached(path, ttl_s)`: la lista de benches (no sus devices, incluido su `id`) se reusa `ttl_s` (30 s); los devices se piden siempre.

En `.flashcfg.json`, un remote de `deploy.py` **sin `host`** (o `"host": "auto"`) se resuelve así: `{"name": "medidor-a", "lock_user": ..., "lock_token": ...}`.
Un solo scan por corrida (`_benches_cache`).

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
- **Escrituras** (`_write_body`): siempre `expect_mac` y el par del lock; `require_reservation: true` si este cliente la reservó (registro local en `~/.cache/espbench/reservations.json`, `ESPBENCH_STATE_DIR`). **Nunca `force`.**
- **Esperas** (`read_range`): poll cada 0,3 s desde el `end` anterior, `echo` hasta `echo_seen`. Los crash (panic, boot_loop) salen de `/events`, no de los `events` de cada respuesta: ver el docstring de `_crash_since` (cursor = inicio de la línea lógica; evento escrito después de su línea).
- **Flash**: `collect_artifact` (solo `flasher_args.json` del build dir) + `flash_one` (protocolo TCP) viven acá; `deploy.py` los importa (con sus prints) y mantiene aparte solo el modo custom.
- **Verify**: primer `boot` después del cursor del flash/reset (si la sesión termina, sigue en la nueva: S3/C3), ventana de asentamiento sin `boot`/`panic`/`boot_loop`, y `until` opcional.

## Tests

`tests/test_espbench_lib.py` (lib), `tests/test_espbench_cli.py` (CLI por subprocess), `tests/test_deploy.py`. Corren contra `tests/benchsim.py`: la API real (adaptador `http.server`, o uvicorn) y una placa con `DeviceManager`/`DeviceLog` reales; tmux y esptool falsos. Para probar a mano: `ESP_BASE=$(mktemp -d) python -m tests.benchsim --port 8099`.

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

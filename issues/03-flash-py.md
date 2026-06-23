# #03 — `server/flash.py` — extraer lógica de flash + tests

## What to build

Extraer la lógica de flash de `server/remote_esp32.py` a `server/flash.py`. Sin cambios funcionales salvo eliminar el hardcodeo de rutas al venv de `sfypi`.

Funciones a mover:
- `find_esptool_cmd() -> list[str]` — eliminar referencias hardcodeadas a `/home/sfypi/espvenv/...`. Solo PATH → `python -m esptool` como fallback.
- `build_esptool_cmd(esptool_cmd, chip, port, baud, encrypt, erase, jobdir) -> tuple[erase_cmd, write_cmd, pairs]`
- `run_cmd(cmd, log) -> int`

Tests en `tests/test_flash.py`:
- `test_build_esptool_cmd_dict`: `flash_files` como dict, verifica pares offset/archivo correctos
- `test_build_esptool_cmd_list`: `flash_files` como list de listas, verifica pares
- `test_build_esptool_cmd_fallback`: `flash_files` ausente, verifica fallback por nombre de archivo (`bootloader.bin`, `app.bin`, etc.)
- `test_build_esptool_cmd_encrypt_flag`: con `encrypt=True`, verifica que `--encrypt` aparece en `write_cmd`
- `test_build_esptool_cmd_erase`: con `erase=True`, verifica que `erase_cmd` no es None

Cada test crea un `jobdir` temporal con archivos `.bin` ficticios y un `flasher_args.json` de ejemplo.

## Acceptance criteria

- [ ] `server/flash.py` existe con las tres funciones
- [ ] `server/remote_esp32.py` importa de `server/flash.py` — no contiene lógica de flash inline
- [ ] No hay rutas hardcodeadas a `/home/sfypi/` en ningún archivo
- [ ] `tests/test_flash.py` pasa (`python -m pytest tests/test_flash.py`)

## Blocked by

- #01

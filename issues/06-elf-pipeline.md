# #06 — `.elf` en artifact + `current.elf` en servidor + tests

## What to build

Pipeline completo para que el monitor pueda decodificar backtraces tras cada flash:

**Cliente (`client/deploy.py`):**
- En `collect_artifact()`, si `is_remote=True`: busca `build/*.elf` (primer match por glob). Si existe, lo agrega al ZIP con nombre `firmware.elf`.
- Si no hay `.elf`: imprime warning, continúa sin él.
- En modo local: no agrega `.elf` (no tiene sentido).

**Servidor (`server/protocol.py` → `handle_control`):**
- Al extraer el ZIP del artifact, si existe `firmware.elf` en el jobdir: lo copia a `{base}/current.elf` (sobreescribe).
- Esto ocurre ANTES de reiniciar el monitor.
- Si no hay `firmware.elf` en el ZIP: no toca `current.elf` (el monitor sigue con el último conocido).

**Monitor (`server/monitor.py`):**
- `EspMonitor` ya acepta `elf_path` desde #05. El servidor lo instancia con `elf_path=base/"current.elf"`.
- En `restart()` post-flash, relanza con el `current.elf` actualizado.

Tests en `tests/test_artifact.py`:
- `test_collect_artifact_remote_includes_elf`: build dir con `.elf`, modo remote → ZIP contiene `firmware.elf`
- `test_collect_artifact_local_excludes_elf`: build dir con `.elf`, modo local → ZIP NO contiene `firmware.elf`
- `test_collect_artifact_no_elf_warning`: build dir sin `.elf`, modo remote → ZIP sin `firmware.elf`, no lanza excepción

## Acceptance criteria

- [ ] Flash remoto con build completo → `{base}/current.elf` actualizado en el servidor
- [ ] Flash remoto sin `.elf` (build parcial) → `current.elf` anterior se preserva, no hay error
- [ ] Monitor reiniciado post-flash usa el `current.elf` actualizado
- [ ] Flash local no incluye `.elf` en el artifact
- [ ] `tests/test_artifact.py` pasa

## Blocked by

- #04
- #05

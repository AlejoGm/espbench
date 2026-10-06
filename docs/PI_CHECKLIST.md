# Checklist de la Pi (espbench para agentes)

Lo que los tests del host no cubren (`docs/ARCHITECTURE.md` §10): correrlo en la Pi después
de cada update grande, en orden. **P0** bloquea el uso; **P1** es lo que valida la feature
con hardware real; **P2** concurrencia y seguridad; **P3** infra que falla poco pero duele.
bench-master y el bench nuevo (identidad, URLs relativas, proxy, `host: auto`): [BENCH_MASTER_TESTING.md](BENCH_MASTER_TESTING.md).

`<dev>` = `device_key`, SN o MAC de una placa; `<tty>` = su `ttyUSBN` / `esp-slotK`. Los comandos
`espbench` corren en la Mac con `ESPBENCH_HOST=<pi>` (o perfil), siempre con `--json`.

## P0 — despliegue

- [ ] `sudo bash remote/infra/update.sh` desde el clone en `feat/agents` (o la rama ya mergeada);
      `cat /opt/esp/VERSION` = el `VERSION` del repo.
- [ ] `/opt/esp/venv/bin/python -c "import regex"` anda. Si no, el install lo avisó
      (`[WARN] no se pudo instalar 'regex'`) y siguió: `/log` usa el fallback (rechaza `(a+)+`).
- [ ] `systemctl is-enabled systemd-time-wait-sync` → `enabled`, con el drop-in
      `/etc/systemd/system/systemd-time-wait-sync.service.d/espbench.conf` (`TimeoutStartSec=90`).
- [ ] `timedatectl`: zona `America/Argentina/Buenos_Aires` (ART, -03) y
      `System clock synchronized: yes` / NTP activo.
- [ ] `devremote --status`: las 3 sesiones `RUNNING` y en `monitoring`.
- [ ] `head -1 /opt/esp/devices/<MAC>/output.log` es el header de sesión
      (`| INFO  | devicelog | sesión <id> tty=…`); después de `devremote --reset <tty>` la anterior
      quedó como `output_<id>.log`.
- [ ] `ls -l /opt/esp/devices/<MAC>/events.jsonl` → `-rw-rw-rw-`, con un evento `session` de la
      sesión actual. `ls -ld /opt/esp/locks` → `drwxrwxrwx`; `locks/*.lck` → `-rw-rw-rw-`.
- [ ] `deploy.py` de `main` y el de esta rama flashean igual (`.flashcfg.json` sin `token` mientras
      no haya `api_token`); un `lock_token` con `:` flashea (en `locks/<tty>` queda como `%3A`).
- [ ] Dashboard con Ctrl-F5: botón **Hora** en el log, pestaña **Eventos**, marcas ⚠/↻ en el vivo,
      contadores de reservas en la home.

- [ ] **Ubicación del bench**: después de `update`, `cat /opt/esp/meta/bench_geo.json` (ciudad de la IP pública, `ts`);
      el header del dashboard la muestra con "auto"; fijar una a mano y volver a "automática" sin 500. Con
      `sudo touch /opt/esp/geo_disabled` y reiniciar el dashboard, `/api/version` da `location: null` (o la manual).
- [ ] **Nota y propiedades con el api como sfypi**: `ls -ld /opt/esp/meta` → `drwxrwxrwx`; desde el dashboard
      (o `espbench note <dev> "x"` / `espbench set <dev> chip=esp32` / `espbench props add uso prueba` y
      `props rm uso prueba`) sin 500; `/opt/esp/meta/properties.json` creado; `devices.json` sigue `-rw-rw-rw-`
      y con los nombres de antes. `journalctl -u dashboard` sin `PermissionError`.

## P1 — hardware

- [ ] `espbench logs <dev> --since boot` y `espbench logs <dev> --around <cursor de un evento>`
      caen en la línea correcta con `esp_idf_monitor` real (sus `\r\n`, colores, backtrace decodificado).
- [ ] **Resets seguidos (C1)**: `espbench flash <dev> --verify` y después `espbench reset <dev> --verify`
      cinco veces seguidas: todas exit 0, sin `boot_loop` en la respuesta. En `run/<tty>.json`,
      `health.boots` vuelve a 1 después de cada reset (el reset pedido al monitor pone los contadores en cero).
- [ ] Botón EN apretado 5 veces en menos de 60 s: aparece un `boot_loop start` (y el `end` un minuto
      después del último). Un `reset --verify` justo cuando el loop arranca da `boot_loop: true`
      informativo, no exit 3.
- [ ] Panic real con backtrace: `espbench send <dev> <comando que crashea> --until OK` → exit 3
      `crashed`, y `espbench logs <dev> --around panic` muestra el backtrace decodificado.
- [ ] `espbench send <dev> help --until idle:500ms` y `--until "<texto de la respuesta>"` con
      `esp_console`: el eco sale como `↪`, el prompt `esp> ` aparece a los ~150 ms, y el `match` es la
      respuesta, no el eco.
- [ ] `espbench flash <dev> --verify` en un ESP32 (UART) y en un S3/C3 (USB-Serial-JTAG): en la
      S3/C3 la placa re-enumera, la respuesta trae `new_session` y el `boot` de la sesión nueva.

## P2 — API, concurrencia y seguridad

- [ ] `send` en loop (200 veces, con el firmware logueando) y después todas las líneas de
      `events.jsonl` son JSON válido:
      `python3 -c "import json,sys; [json.loads(l) for l in open(sys.argv[1])]" events.jsonl`.
- [ ] Reservas: `espbench reserve <dev>` + replug → sigue reservada; otra placa en el mismo puerto →
      la reserva se borra al arrancar; otro usuario → `send` da 423 `locked` y `flash` `locked`;
      `--ttl 25h` → 400 `bad_request` (tope 24 h).
- [ ] Tres agentes a la vez (cada uno con su `ESPBENCH_USER`, `send --until` en loop): CPU de
      uvicorn en `top`, y el dashboard sigue respondiendo (log en vivo, eventos).
- [ ] Token: crear `/opt/esp/api_token` (`chown root:sfypi`, `chmod 640`, ver README "Despliegue y
      seguridad") → el dashboard pide el token; un `.flashcfg.json` sin `remote.token` falla con
      `unauthorized`; con el token flashea; aparece **Forzar**; `devremote --unlock <tty>` deja un
      `release` con `by_host: devremote`. Al terminar, borrar el archivo (o dejarlo, si se decide activarlo).

## P3 — infra

- [ ] Reboot sin red: `devremote` arranca en ≤ 90 s. Reboot con red: espera a NTP (la hora del header
      de la primera sesión es la correcta).
- [ ] Hotplug: desenchufar y enchufar → sesión nueva sola; slots udev (`esp-slotK` estable, si hay
      `slots.conf`); `espbench restart-session <dev>` relanza el proceso (sesión nueva).
- [ ] Firmware en boot loop 5 min: `events.jsonl` crece poco (un `boot_loop start` y un `end` con
      `panics`, no un evento por panic) y el poll (`espbench logs <dev> --since now --until x --timeout 2s`)
      no se pone más lento.
- [ ] `devremote --cleanup` y después `espbench logs <dev> --around <cursor de una sesión borrada>` →
      `cursor_expired` (esperado: los eventos quedan, el log no).

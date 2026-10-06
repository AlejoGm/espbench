# Guía de prueba: bench nuevo + bench-master

Cómo probar la rama `feat/agents-bench-master` de punta a punta: primero sin hardware (en la Mac), después un
bench real instalado de cero, y por último bench-master contra los benches de la tailnet. Lo específico de agentes
(`espbench`, reservas, eventos, token) está en [PI_CHECKLIST.md](PI_CHECKLIST.md); acá solo se repite lo que
cambia al pasar por bench-master.

Convenciones:

- `$EB` = el repo en la Mac, en esta rama. Si usás el worktree:
  `EB=~/Dev/espbench/.claude/worktrees/agents-bench-master`
- `<pi>` = un bench (`sensipi02`, o su IP de Tailscale `100.x.y.z`).
- `<dev>` = `device_key`, SN o MAC de una placa; `<tty>` = su `ttyUSBN` / `esp-slotK`.

---

## 0. Antes de empezar (Mac)

```bash
tailscale status                       # la Mac conectada; los benches con su IP 100.x
curl -s http://<pi>:8080/api/version   # llega al bench (hoy: {"version": ...} si todavía no se actualizó)
```

Si `curl` no llega: `tailscale ping <pi>` (llega por la tailnet?) y, en el bench, `systemctl status dashboard`.

---

## 1. Sin hardware (Mac)

### 1.1 Tests

```bash
cd $EB
python3 -m pytest tests/ -q                         # todo menos bench-master (sin httpx se saltea)
master/bench-master --help                          # la primera vez arma master/.venv
master/.venv/bin/python -m pytest tests/ -q         # todo, incluido tests/test_master.py
```

Esperado: todo verde (hoy: 606 sin bench-master, 624 con el venv del master).

### 1.2 Bench simulado detrás de bench-master

Tres terminales:

```bash
# 1) bench simulado (API real + placa simulada), en :18099
cd $EB && ESP_BASE=$(mktemp -d) master/.venv/bin/python -m tests.benchsim --uvicorn --port 18099
```

```bash
# 2) bench-master apuntando solo al simulado (sin Tailscale)
cd $EB
echo '{"tailscale": false, "hosts": ["127.0.0.1:18099"]}' > /tmp/benches-sim.json
ESPBENCH_BENCHES_CONFIG=/tmp/benches-sim.json master/bench-master --port 18091 --open
```

```bash
# 3) chequeos por API
curl -s localhost:18091/api/benches | python3 -m json.tool
curl -s localhost:18091/api/devices | python3 -m json.tool
curl -s localhost:18091/api/resolve/sim-board | python3 -m json.tool
```

En el browser (`http://localhost:18091`):

- [ ] Una sección por bench, con su nombre, versión, `config · 127.0.0.1` y la card de `sim-board`.
- [ ] **Ver monitor** → abre `/bench/<nombre>/device.html?...`: log en vivo, botón **Hora**, pestaña **Eventos**.
- [ ] Consola: mandar `help` → aparece el eco y la respuesta, y un evento `send` en Eventos.
- [ ] Cortar la terminal 1 (Ctrl-C): a los ~5 s el bench pasa a **offline, visto hace…** con la card
      atenuada; el header cuenta `1 offline`. Volver a levantarlo → vuelve a online.

Cortar todo con Ctrl-C al terminar.

---

## 2. Bench real: instalación desde cero

El clone va en `/opt/espbench` (de root); `install.sh` lo anota en `/opt/esp/update.conf` y de ahí en más el bench
se actualiza solo. En el bench (por ssh):

```bash
# 1. Parar todo
sudo systemctl stop dashboard devremote
sudo -u sfypi tmux kill-server 2>/dev/null; sudo pkill -f remote_esp32.py

# 2. Backup de los nombres de las placas
sudo cp /opt/esp/devices.json ~/devices.json.bak

# 3. Borrar lo viejo (el toolchain se queda: son ~100 MB que el install volvería a bajar)
sudo rm -rf /opt/espbench ~/espbench
sudo find /opt/esp -mindepth 1 -maxdepth 1 ! -name toolchain -exec rm -rf {} +

# 4. Clone en la rama a probar + install (queda con PIN en esa rama: el update automático no la pisa)
sudo git clone -b feat/agents-bench-master https://github.com/AlejoGm/espbench.git /opt/espbench
sudo bash /opt/espbench/remote/install.sh

# 5. Nombres de vuelta, nombre del bench y arrancar
sudo cp ~/devices.json.bak /opt/esp/devices.json && sudo chmod 666 /opt/esp/devices.json
echo "sensipi04" | sudo tee /opt/esp/bench_name       # opcional: sin esto, el hostname
sudo systemctl restart dashboard && sudo systemctl start devremote
```

El paso 3 borra para siempre logs, historial de flasheos y jobs viejos: copiar antes lo que se quiera guardar.

Checks en el bench:

- [ ] `cat /opt/esp/VERSION` = el `VERSION` del repo.
- [ ] `cat /opt/esp/update.conf` → `REPO_DIR=/opt/espbench` y `PIN=feat/agents-bench-master`.
- [ ] `systemctl list-timers espbench-update.timer` → programado (boot + 04:00).
- [ ] `devremote --status`: todas las sesiones `RUNNING`.
- [ ] Lo de **P0** de [PI_CHECKLIST.md](PI_CHECKLIST.md) (regex, time-sync, header de sesión, events.jsonl).

Desde la Mac:

```bash
curl -s http://<pi>:8080/api/version
# {"app":"espbench","version":"0.33.0","name":"sensipi04","auth":false}
curl -s http://<pi>:8080/api/update
# {"version":"0.33.0","pin":"feat/agents-bench-master","status":null}
```

### 2.1 Dashboard directo (que las URLs relativas no rompieron nada)

`http://<pi>:8080` con **Ctrl-F5** (o Cmd-Shift-R):

- [ ] Home: cards, contadores, renombrar una placa (✎) funciona.
- [ ] **⎘ Config** copia `"host": "<pi>"` (entrando directo, no `auto`).
- [ ] Monitor: log en vivo, historial, descargar log (⤓), consola, Reset/Boot.
- [ ] DevTools → Console sin errores; Network sin 404 (sobre todo `style.css`, `espbench.js`, `auth.js`).

---

## 3. bench-master contra los benches reales

```bash
cd $EB && master/bench-master --open        # http://localhost:8090
```

Sin config usa todos los peers online de Tailscale. Para sumar benches fuera de la tailnet:

```bash
cat > ~/.config/espbench-benches.json <<'EOF'
{"tailscale": true, "hosts": ["192.168.1.50"], "timeout_s": 2}
EOF
```

### 3.1 Discovery

- [ ] Aparece cada bench online, con su nombre (`bench_name` o hostname), versión y `tailscale · 100.x.y.z`.
- [ ] **No** aparecen las PCs de la tailnet que no son benches.
- [ ] Un bench todavía sin actualizar aparece igual, con el nombre de Tailscale (ver 3.4).
- [ ] Mismo bench por dos caminos: poner su IP de LAN en `hosts` → sigue apareciendo **una** vez
      (gana `config`). Sacarla después.
- [ ] Bench nuevo enchufado a la tailnet: aparece solo (o al toque con el botón ↻).

```bash
curl -s localhost:8090/api/benches | python3 -m json.tool
```

### 3.2 Grilla y proxy

- [ ] Contadores del header: benches, offline, devices, ok/ocupados/con problemas/caídos, reservadas, lock de flash.
- [ ] Búsqueda (`/`) por nombre, bench, SN, MAC, firmware.
- [ ] Filtro por bench (chips arriba, si hay más de uno).
- [ ] **Ver monitor** de una placa de cada bench → log en vivo a través de `localhost:8090/bench/<nombre>/...`.
- [ ] Consola por el proxy: `help` llega a la placa (eco + respuesta en el log).
- [ ] Reset y Boot desde el monitor por el proxy.
- [ ] Historial: flasheos con su log; sesiones anteriores, ver y descargar (⤓).
- [ ] Pestaña Eventos por el proxy.
- [ ] **dashboard del bench →** abre la home del bench por el proxy; desde ahí, renombrar una placa funciona.
- [ ] **↗** abre el monitor directo en `http://<ip>:8080/...`.

### 3.3 Bench caído

En el bench:

```bash
sudo systemctl stop dashboard
```

- [ ] En ~5–10 s: **offline, visto hace…**, cards atenuadas con el último estado conocido, `1 offline` en el header.
- [ ] `curl -s localhost:8090/api/resolve/<dev-de-ese-bench>` → 404 (no se resuelve contra benches caídos).

```bash
sudo systemctl start dashboard        # vuelve a online en el próximo poll
```

### 3.4 Bench sin actualizar (mezcla de versiones)

Con un bench todavía en `main` o `feat/agents`:

- [ ] Aparece en la grilla con sus devices.
- [ ] **Ver monitor** por el proxy **no** anda (ese frontend usa rutas absolutas): es lo esperado; **↗** sí.

### 3.5 Guard del master (CSRF / DNS rebinding)

```bash
curl -s -o /dev/null -w "%{http_code}\n" localhost:8090/api/benches                              # 200
curl -s -o /dev/null -w "%{http_code}\n" -H "Host: evil.example" localhost:8090/api/benches      # 403
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Origin: https://evil.example" \
     localhost:8090/bench/<nombre>/api/device/<tty>/command/reset                               # 403
```

El tercero **no** tiene que resetear la placa (mirar el log).

### 3.6 Token de la API a través del master (si un bench tiene `/opt/esp/api_token`)

- [ ] Una escritura por el proxy (consola) pide el token una vez; después anda.
- [ ] Otro bench con otro token lo pide aparte (el token se guarda por bench, no se mezcla).

---

## 4. `deploy.py` con `host: auto`

En un proyecto ESP-IDF, un remote **sin `host`** en `.flashcfg.json` (o copiarlo con **⎘ Config** desde bench-master):

```json
{
  "mode": "remote",
  "remote": [{"name": "<dev>", "lock_user": "alejo", "lock_token": "<tu-lock-token>"}],
  "chip": "esp32",
  "flash_baud": 921600
}
```

```bash
python3 $EB/client/deploy.py --no-build     # flashea el build que ya está
```

- [ ] Imprime `[REMOTE] Buscando benches...` y `[REMOTE] '<dev>' está en el bench <nombre> (<ip>), <tty>`, y flashea.
- [ ] Con dos remotes en benches distintos: un solo "Buscando benches" y flashea los dos.
- [ ] `name` que no existe → error con la lista de benches encontrados.
- [ ] Ambiguo: renombrar (✎) dos placas de benches distintos con el mismo `device_key` → error que nombra
      `<bench>/<tty>` de las dos. Usar `"name": "<bench>/<tty>"` para elegir una. Después, volver a
      renombrarlas como estaban.
- [ ] Un remote con `host` explícito sigue andando igual que antes.

`resolve` sin flashear:

```bash
curl -s localhost:8090/api/resolve/<dev> | python3 -m json.tool
python3 -c "import sys; sys.path.insert(0, '$EB'); from client import benches; b, d = benches.resolve('<dev>'); print(b.name, b.address, d['port_tcp'])"
```

---

## 5. Updates del bench (`espbench-update`)

En el bench:

```bash
sudo espbench-update                         # al PIN (lo último de feat/agents-bench-master): up_to_date si no hay nada
cat /opt/esp/update_status.json; tail -20 /opt/esp/update.log
sudo systemctl start espbench-update.service # lo que corre el timer (--auto): con PIN → skipped
```

- [ ] `espbench-update` sin cambios → `state: up_to_date`, no reinicia nada.
- [ ] Pushear un commit a la rama y `sudo espbench-update` → `state: ok`, `/api/version` con la versión nueva,
      sesiones reiniciadas (`devremote --status`).
- [ ] `--auto` con PIN → `skipped` ("fijo en ...").
- [ ] Con una placa reservada (`espbench reserve <dev>`): `--auto` sin PIN → `skipped` (ocupado); manual → `failed`
      salvo `--force`.
- [ ] **Rollback**: rama de prueba con un `remote/server/api.py` roto a propósito (por ejemplo un `raise` al importar)
      y `sudo espbench-update --ref <esa-rama>` → `state: rolled_back`, el bench vuelve a la versión anterior y el
      dashboard anda. Borrar la rama después.

Desde bench-master:

- [ ] La cabecera del bench muestra `📌 feat/agents-bench-master`.
- [ ] **⟳ update** con la misma rama → a los segundos `⟳ actualizando…`, el bench se va offline un momento (reinicia
      el dashboard) y vuelve con `✓ actualizado`.
- [ ] **⟳ update** con una ref que no existe → el bench queda igual y el estado dice `✗ update falló`.
- [ ] Con `api_token` en el bench: lo pide una vez.

Releases (cuando haya uno, desde main):

```bash
git tag v$(cat VERSION) && git push origin v$(cat VERSION)
```

- [ ] Un bench sin PIN (`sudo espbench-update --release`) pasa al tag; con el timer, esa misma noche.

---

## 6. Seguridad de Tailscale (cuando se decida aplicarla)

Detalle y JSON de la política en [security.md](security.md). Orden sugerido:

1. Aplicar la política en el panel de Tailscale (fusionada con la actual).
2. Pasar **un** bench al tag: `sudo tailscale up --force-reauth --advertise-tags=tag:bench` (lo aprueba un admin).
3. Desde la Mac: `curl -s http://<pi>:8080/api/version` sigue andando; bench-master lo sigue viendo.
4. Desde ese bench (no tiene que llegar a nada):
   ```bash
   nc -zv -w3 <ip-tailscale-de-tu-mac> 22
   curl -m3 http://<ip-de-otro-bench>:8080/api/version
   ```
5. Si todo bien, migrar el resto. Para benches nuevos: `sudo TS_AUTHKEY=tskey-auth-... bash rpi/pi-setup.sh`.

---

## Si algo falla

| Síntoma | Mirar |
|---|---|
| bench-master no encuentra ningún bench | `tailscale status` (¿Mac conectada? ¿bench online?), `curl <pi>:8080/api/version` desde la Mac, `ESPBENCH_BENCHES_CONFIG` apuntando a otro archivo |
| Aparece un host que no es bench | Contestó `/api/version` con `{"version"}` y nada más (se toma como bench viejo): avisar |
| Directo en el bench se ve el monitor viejo (cabecera `key`/`version`, sin Hora ni consola) y por bench-master el nuevo | Caché del navegador de la versión anterior (el dashboard viejo no mandaba `Cache-Control`): Cmd+Shift+R en la home y en el monitor del bench. Pasa una vez por bench, después de la primera instalación |
| Monitor por el proxy en blanco | Ctrl-F5; DevTools → Network: ¿algún pedido a `/api/...` sin el `/bench/<nombre>/`? Ese bench tiene el frontend viejo |
| Escritura por el proxy da 403 | El guard: ¿la página está en `localhost:8090` y no en otro nombre/IP de la Mac? |
| Log del master | La terminal donde corre `master/bench-master` (errores de scan con `[bench-master]`) |
| Log del bench | `journalctl -u dashboard -n 100` en el bench |

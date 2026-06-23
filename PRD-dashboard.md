# PRD — Dashboard Web (espbench)

> Estado: ready-for-implementation
> Fecha: 2026-06-23
> Contexto: diseño por grill-me + análisis de codebase

---

## Problem Statement

Hoy, para ver la salida serial de un ESP32 conectado a la Raspberry Pi hay que hacer SSH + `devremote <N>` para adjuntarse a la sesión tmux correcta. Si hay más de un dispositivo conectado, no hay visibilidad centralizada del estado del sistema. Cuando se reconecta un ESP32, el número de ttyUSBX puede cambiar y el `.flashcfg.json` del proyecto queda apuntando al puerto equivocado, obligando a corregirlo manualmente. No existe forma de ver el monitor serial desde una segunda máquina o de forma remota sin SSH.

---

## Solution

Un servidor HTTP corriendo en la Raspberry Pi que expone un dashboard web accesible desde cualquier browser en la red local (o vía VPN). El home muestra una card por dispositivo conectado con su estado y metadata. Desde cada card se accede al detalle: toda la salida serial del día en tiempo real, con scroll libre, sin necesidad de tmux.

---

## User Stories

1. Como desarrollador, quiero abrir un browser y ver qué ESP32 están conectados a la Pi sin hacer SSH, para tener visibilidad del sistema de un vistazo.

2. Como desarrollador, quiero ver en cada card el CHIPID del dispositivo Sensify (parseado del boot), para identificar qué hardware físico es cada ttyUSBX sin depender del número de puerto.

3. Como desarrollador, quiero ver en cada card el estado RUNNING/DOWN del monitor serial, para saber si el servidor de flash está activo para ese dispositivo.

4. Como desarrollador, quiero ver en cada card el timestamp del último flash realizado, para saber cuándo se actualizó el firmware por última vez.

5. Como desarrollador, quiero clickear una card y ver la salida serial completa del día en el browser, para no tener que adjuntarme a tmux.

6. Como desarrollador, quiero que al abrir la vista de detalle de un dispositivo, el log aparezca posicionado al final (lo más reciente), para ver la actividad actual inmediatamente.

7. Como desarrollador, quiero poder hacer scroll hacia arriba en la vista de detalle y ver todo el historial del día, para diagnosticar problemas ocurridos antes de conectarme.

8. Como desarrollador, quiero que la vista de detalle reciba nuevas líneas en tiempo real vía WebSocket, para no tener que refrescar la página.

9. Como desarrollador, quiero que el dashboard detecte automáticamente los dispositivos conectados al Pi leyendo los `/dev/ttyUSBX` presentes, sin configuración manual.

10. Como desarrollador, quiero que el CHIPID se detecte automáticamente al parsear la salida serial del boot (`CHIPID = <número>`), sin tener que ingresarlo a mano.

11. Como desarrollador, quiero que si el CHIPID no se detectó aún (dispositivo recién conectado, firmware no arrancó), la card muestre el ttyUSBX como identificador provisorio.

12. Como desarrollador, quiero que el dashboard corra como un proceso independiente del servidor de flash, para poder reiniciarlo sin interrumpir sesiones activas.

13. Como desarrollador, quiero que `install.sh` despliegue el dashboard junto con el servidor de flash, para que el setup siga siendo reproducible con un solo comando.

14. Como desarrollador, quiero acceder al dashboard desde mi laptop sin VPN cuando estoy en la misma red, y con VPN cuando estoy remoto, sin auth adicional.

15. Como desarrollador, quiero que el puerto del dashboard sea configurable, para evitar conflictos con otros servicios en la Pi.

---

## Implementation Decisions

### Proceso independiente

El dashboard corre como un proceso FastAPI/uvicorn separado del `remote_esp32.py`. Se comunica con el sistema de flash solo a través del filesystem (logs, jobs directory) — no comparte memoria ni IPC. Esto permite reiniciar el dashboard sin afectar flasheos en curso.

### Stack

- **Backend**: FastAPI + uvicorn (async, WebSocket nativo)
- **Frontend**: HTML + JavaScript vanilla, servido como archivos estáticos desde FastAPI
- **No React ni build pipeline** — los archivos estáticos se copian directamente en el deploy

### Módulos nuevos

**`server/device_registry.py`** — Fuente de verdad del estado de dispositivos.
- Lista `/dev/ttyUSB*` presentes en el sistema
- Lee el directorio de jobs para obtener timestamp del último flash por dispositivo
- Consulta `tmux has-session` para estado RUNNING/DOWN por sesión `esp32_ttyUSBX`
- Cachea CHIPIDs parseados en memoria (se populan vía eventos del log streamer)
- Interfaz: `list_devices() → list[DeviceInfo]`, `get_device(tty) → DeviceInfo`

**`server/log_streamer.py`** — Streaming de log files a clientes WebSocket.
- Abre el log file del día actual para un ttyUSBX dado
- Envía el contenido completo al conectar un cliente nuevo
- Hace tail en vivo (lectura async con polling) y broadcast a todos los clientes suscritos al mismo dispositivo
- Maneja desconexiones de clientes y rotación de log file (si el monitor se reinicia y cambia de archivo)
- Interfaz: `LogStreamer.subscribe(tty, websocket)`, `LogStreamer.unsubscribe(tty, websocket)`

**`server/dashboard.py`** — FastAPI app.
- `GET /` → sirve `index.html`
- `GET /api/devices` → JSON con lista de DeviceInfo
- `GET /api/device/{tty}` → JSON con DeviceInfo de un dispositivo
- `WebSocket /ws/device/{tty}` → stream del log en tiempo real

**`dashboard/`** — Archivos estáticos frontend.
- `index.html` — home con cards de dispositivos, polling a `/api/devices` cada 5s
- `device.html` — detalle de un dispositivo, WebSocket conectado a `/ws/device/{tty}`
- `style.css` — estilos mínimos, sin frameworks CSS externos

### Path del log file

El log streamer resuelve el path del día actual como: `/opt/esp/logs/YYYYMMDD/serial.log` donde `YYYYMMDD` es la fecha actual. Si el archivo no existe aún (monitor no arrancó), envía string vacío y espera a que se cree.

### CHIPID parsing

El log streamer busca en cada chunk de log el patrón `CHIPID = (\d+)` y notifica al device registry cuando lo detecta. El registry lo persiste en memoria para el tty correspondiente. No se persiste a disco en el MVP.

### Deploy

- `install.sh` crea `/opt/esp/dashboard/` y copia los archivos estáticos
- `install.sh` instala `infra/dashboard.service` (systemd unit) para levantar uvicorn al boot
- Puerto default: 8080

### Dependencias Python nuevas

```
fastapi
uvicorn[standard]
```

Se agregan a `requirements.txt` — el venv existente en `/opt/esp/venv` ya las instala.

---

## Testing Decisions

Los tests buenos verifican comportamiento observable desde el exterior del módulo, no implementación interna.

**`server/device_registry.py`** — testeable en host sin hardware:
- `list_devices()` con directorio `/dev` mockeado (o tmpdir con archivos `ttyUSB*` falsos)
- Detección de CHIPID al recibir una línea de log con el patrón correcto
- Estado DOWN cuando no hay sesión tmux activa (mock de subprocess)

**`server/log_streamer.py`** — testeable en host con archivos temporales:
- Al suscribir un cliente nuevo, recibe el contenido completo del archivo existente
- Nuevas líneas escritas al archivo se reciben por el WebSocket
- Desconexión de cliente no rompe el streamer

**Patrón de tests**: mismo estilo que `tests/test_common.py` y `tests/test_flash.py` — pytest, sin mocks innecesarios, usando filesystem real en directorios temporales.

---

## Out of Scope

- Auth (usuario/contraseña o token) — red local + VPN es suficiente
- HTTPS
- Flash desde el browser — el deploy sigue siendo `deploy.py` desde el repo del proyecto
- Historial de días anteriores (selector de fecha)
- Nombre de proyecto asignado manualmente a un CHIPID (Fase 2)
- Tracking CHIPID → ttyUSBX persistido a disco (Fase 2)
- Detección de panics y alertas (Fase 2)
- Estado MQTT parseado del serial (Fase 2)
- Endpoint REST para CI (Fase 3)
- Tests de integración con hardware real (Fase 3)

---

## Further Notes

El buffer circular de 64 KB que `EspMonitor` ya mantiene en memoria es la base natural para la Fase 2 (alertas de panics, estado MQTT). En la Fase 1, el dashboard lee el log file en disco — más simple y desacoplado.

La Fase 2 añadirá un mecanismo para que el `LogStreamer` notifique al `DeviceRegistry` sobre eventos semánticos (CHIPID detectado, panic detectado, estado MQTT). Diseñar `LogStreamer` con un hook de observer en mente facilita esta extensión sin romper la interfaz actual.

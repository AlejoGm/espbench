# #11 — `server/log_streamer.py` — tail async + WebSocket broadcast + CHIPID parsing

## What to build

Módulo que hace tail del log file de un dispositivo y broadcast a todos los clientes WebSocket suscritos. También parsea el log buscando `CHIPID = \d+` y notifica al DeviceRegistry.

Funcionalidad:
- `LogStreamer` — clase con un streamer por ttyUSBX
- `subscribe(tty, websocket)`: al conectar, envía el contenido completo del log del día, luego stream en vivo
- `unsubscribe(tty, websocket)`: limpieza al desconectar el cliente
- Tail async: leer el archivo con polling (asyncio, sin inotify para mantener compatibilidad)
- Parsear cada chunk buscando `CHIPID = (\d+)` — si detecta, llamar a `DeviceRegistry.set_chip_id(tty, chip_id)`
- `WebSocket /ws/device/{tty}` en `dashboard.py` usa este módulo

## Tests (`tests/test_log_streamer.py`)

- Al suscribir un nuevo cliente, recibe el contenido completo del archivo existente
- Líneas escritas al archivo después de suscribir llegan al WebSocket
- CHIPID detectado en log llama a `set_chip_id` con el valor correcto
- Desconexión de un cliente no rompe el streamer ni afecta otros clientes suscritos
- Si el log file no existe al suscribir, el cliente recibe string vacío y empieza a recibir cuando el archivo se crea

## Acceptance criteria

- [ ] Conectar un WebSocket a `/ws/device/ttyUSB0` entrega todo el log actual y luego stream en vivo
- [ ] Múltiples clientes suscritos al mismo tty reciben los mismos mensajes
- [ ] `CHIPID = 123456` en el log → `DeviceRegistry.chip_id` para ese tty vale `"123456"`
- [ ] Todos los tests pasan en host sin hardware

## Blocked by

#10

# #13 — Dashboard: vista de detalle del dispositivo (monitor serial)

## What to build

Frontend de la vista de detalle: log completo del día + stream en vivo vía WebSocket.

Funcionalidad:
- `dashboard/device.html` — página de detalle, recibe `?tty=ttyUSBX` como query param
- Al cargar: conecta WebSocket a `/ws/device/{tty}`
- Primer mensaje del WebSocket = contenido completo del log del día → renderizar en el `<div>` del terminal
- Mensajes siguientes = nuevas líneas → append al final
- Auto-scroll al fondo al recibir nuevas líneas, EXCEPTO si el usuario scrolleó hacia arriba (no interrumpir lectura de historial)
- Fuente monospace, fondo oscuro (similar a terminal)
- Header con: identificador del dispositivo, estado, botón "← Volver"
- Reconexión automática si cae el WebSocket (retry con backoff)

## Acceptance criteria

- [ ] Al abrir `device.html?tty=ttyUSB0` se ve todo el log del día
- [ ] Nuevas líneas aparecen en tiempo real sin reload
- [ ] Auto-scroll al fondo con nuevas líneas, pero se detiene si el usuario scrolleó hacia arriba
- [ ] Si el usuario scrollea al fondo manualmente, el auto-scroll se reactiva
- [ ] Reconexión automática tras desconexión del WebSocket
- [ ] Renderizado en fuente monospace legible

## Blocked by

#11 (necesita WebSocket `/ws/device/{tty}` funcionando)

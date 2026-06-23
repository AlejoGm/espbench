# #12 — Dashboard: home UI con cards de dispositivos

## What to build

Frontend del home: cards de dispositivos con polling a `/api/devices` cada 5 segundos.

Funcionalidad:
- `dashboard/index.html` — reemplaza el placeholder del #09
- Una card por dispositivo con: identificador (CHIPID si disponible, sino ttyUSBX), estado RUNNING/DOWN con color (verde/rojo), puerto TCP, timestamp del último flash
- Botón "Ver monitor" en cada card → navega a `device.html?tty=ttyUSBX`
- Polling cada 5s a `/api/devices` — actualiza cards sin reload de página
- Si no hay dispositivos conectados: mensaje "No hay dispositivos conectados"
- Sin frameworks JavaScript externos — vanilla fetch + DOM

## Acceptance criteria

- [ ] Home muestra una card por cada dispositivo en `/api/devices`
- [ ] RUNNING en verde, DOWN en rojo
- [ ] Timestamp del último flash legible (fecha + hora)
- [ ] "Ver monitor" navega a la página de detalle del dispositivo
- [ ] Si `chip_id` está disponible se muestra como título de la card; si no, se muestra `ttyUSBX`
- [ ] Sin dispositivos → mensaje visible

## Blocked by

#10 (necesita `/api/devices` con datos reales)

# #09 — Dashboard: scaffold FastAPI + static files

## What to build

Estructura base del dashboard: FastAPI app, archivos estáticos frontend, systemd service, deploy en `install.sh`.

Acciones:
- Crear `server/dashboard.py` con FastAPI app mínima: `GET /` sirve `dashboard/index.html`, `GET /api/devices` retorna `[]` hardcodeado
- Crear `dashboard/index.html` — página mínima que hace fetch a `/api/devices` y muestra el resultado en pantalla
- Crear `dashboard/style.css` — estilos base (sin frameworks externos)
- Crear `infra/dashboard.service` — systemd unit que lanza `uvicorn server.dashboard:app --host 0.0.0.0 --port 8080` desde `/opt/esp`
- Agregar `fastapi` y `uvicorn[standard]` a `requirements.txt`
- Actualizar `install.sh`: copiar `dashboard/` → `/opt/esp/dashboard/`, instalar `dashboard.service`, habilitar el servicio

## Acceptance criteria

- [ ] `cd /opt/esp && uvicorn server.dashboard:app` arranca sin errores
- [ ] `GET http://pi:8080/` retorna HTML
- [ ] `GET http://pi:8080/api/devices` retorna `[]`
- [ ] `sudo systemctl start dashboard` levanta el proceso
- [ ] `install.sh` despliega todo idempotentemente

## Blocked by

None — puede empezar inmediatamente.

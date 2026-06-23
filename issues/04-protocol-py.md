# #04 — `server/protocol.py` — extraer handler TCP

## What to build

Extraer el servidor TCP de `server/remote_esp32.py` a `server/protocol.py`. Usa `common.send_msg`/`recv_msg` en lugar del framing inline actual.

Funciones a mover:
- `handle_control(sock, cfg, mon, svc_log)` — lógica completa de una sesión: recibir header, ACK, artifact, verificar SHA256, extraer ZIP, delegar flash, responder resultado
- `control_server(cfg, mon, svc_log)` — loop TCP accept, llama a `handle_control` por conexión

`server/remote_esp32.py` queda como entrypoint puro: argparse, setup de directorios, instanciación de `EspMonitor`, arranque del hilo TCP, loop de espera + signal handler.

No hay cambios funcionales al protocolo. Verificación: un flash remoto end-to-end produce el mismo resultado JSON que antes.

## Acceptance criteria

- [ ] `server/protocol.py` existe con `handle_control` y `control_server`
- [ ] `server/remote_esp32.py` tiene menos de 80 líneas (solo entrypoint)
- [ ] No hay framing TCP inline en `remote_esp32.py`
- [ ] Flash remoto end-to-end funciona igual que antes

## Blocked by

- #02
- #03

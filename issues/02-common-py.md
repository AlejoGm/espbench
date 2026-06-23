# #02 — `common.py` — framing TCP + sha256 + tests

## What to build

Crear `common.py` en la raíz con las dos utilidades que hoy están duplicadas entre cliente y servidor. Actualizar ambos para importar desde ahí.

Funciones a extraer:
- `sha256_file(path: Path) -> str` — hoy duplicada con idéntica implementación en cliente y servidor
- `send_msg(sock, obj: dict)` — encapsula `struct.pack(">I", len) + json.dumps + sendall`
- `recv_msg(sock) -> dict` — encapsula `recv 4 bytes + unpack len + recv n bytes + json.loads`

En el servidor, `recv_exact` y el framing inline en `handle_control` se reemplazan por `recv_msg`/`send_msg`.
En el cliente, `recvall` + el framing inline en `flash_remote` se reemplazan por los mismos.

Tests en `tests/test_common.py`:
- `test_sha256_file`: crea archivo temp con contenido conocido, verifica digest exacto
- `test_send_recv_roundtrip`: usa `socket.socketpair()`, envía dict arbitrario, verifica que se recibe igual
- `test_recv_msg_closed_socket`: verifica que `recv_msg` lanza excepción si el socket cierra a mitad

## Acceptance criteria

- [ ] `common.py` existe con `sha256_file`, `send_msg`, `recv_msg`
- [ ] `server/remote_esp32.py` no contiene framing TCP inline ni `recv_exact`
- [ ] `client/deploy.py` no contiene `recvall` ni framing TCP inline
- [ ] `tests/test_common.py` pasa (`python -m pytest tests/test_common.py`)
- [ ] Flash remoto funciona igual que antes (comportamiento sin cambios)

## Blocked by

- #01

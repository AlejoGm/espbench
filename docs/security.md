# Seguridad de los benches

Los benches pueden quedar en lugares que no controlamos (en un cliente, en otra oficina). Hay que partir de
este supuesto: **quien tiene el bench en la mano tiene la SD**. Cifrarla no sirve sin TPM: el bench
necesitaría la clave para bootear solo. El objetivo es otro: que robar un bench no valga más que ese bench.

## Qué hay en un bench

| Qué | Riesgo | Estado |
|---|---|---|
| Clave de nodo de Tailscale (`/var/lib/tailscale/`) | **Alto**: entrar a la tailnet como ese nodo | Con `tag:bench` + la ACL de abajo, ese nodo no llega a nadie |
| Credenciales de git | — | No hay: el repo es público y `update.sh` hace pull por https |
| WiFi del lugar (portal de provisioning) | Es del sitio donde está el bench | Inevitable |
| Firmware: bins de los jobs y `current.elf` en `/opt/esp/devices/<mac>/` | Propiedad intelectual (el `.elf` trae símbolos) | Pendiente: no guardar bins tras el flash; decodificar backtraces del lado del dev |
| `token` / `lock_token` | Bajo si son distintos por bench | Usar valores por bench, no compartidos con otros sistemas |

## Tailscale: benches con tag y ACL de una sola dirección

Un nodo que entra con `tailscale up` interactivo queda **a nombre del usuario** que lo autenticó y tiene sus
permisos. Con la política por defecto (todos ven a todos), un bench robado ve la Mac del dev, las PCs del
equipo y todo lo demás. Con un tag:

- el nodo no pertenece a nadie y no hereda permisos de un usuario;
- la ACL deja que los humanos lleguen a los benches, y **que los benches no inicien conexiones a nadie**;
- las claves de nodos con tag no vencen solas (bien para un equipo desatendido). Si se pierde un bench, se lo
  borra en el panel de Tailscale y queda revocado.

### Política (admin de Tailscale → Access controls)

Hay que fusionarla con la política actual. `autogroup:member → autogroup:member` mantiene lo que los humanos
ya tienen entre sí; si la política actual es más restrictiva, quedarse con esa parte.

```jsonc
{
  "tagOwners": {
    "tag:bench": ["autogroup:admin"]
  },
  "grants": [
    // Humanos → benches: ssh, dashboard (8080) y flasheo (5000-5199, ver ARCHITECTURE §6).
    {"src": ["autogroup:member"], "dst": ["tag:bench"], "ip": ["tcp:22", "tcp:8080", "tcp:5000-5199"]},
    // Humanos entre sí, como hasta ahora.
    {"src": ["autogroup:member"], "dst": ["autogroup:member"], "ip": ["*"]}
    // Ninguna regla con src tag:bench: un bench no inicia conexiones a nadie de la tailnet.
  ]
}
```

Para chequear después de aplicarla: desde un bench, `nc -zv -w3 <ip-tailscale-de-tu-mac> 22` y
`curl -m3 <ip-de-otro-bench>:8080/api/version` tienen que fallar (`tailscale ping` no sirve: pasa aunque la ACL bloquee). Desde la Mac, `curl <ip-del-bench>:8080/api/version` tiene que andar.

### Alta de un bench nuevo

1. Admin → Settings → Keys → *Generate auth key*: **un solo uso**, **no efímera**, tag `tag:bench`, vencimiento
   corto (ese vencimiento es el de la clave de alta, no el del nodo).
2. `sudo TS_AUTHKEY=tskey-auth-... bash rpi/pi-setup.sh`. La clave va por variable de entorno: por argumento
   quedaría en `ps` y en el historial. No se guarda en disco.

### Pasar al tag un bench que ya está en la tailnet

```bash
sudo tailscale up --force-reauth --advertise-tags=tag:bench
```

Abre un link de login. Quien lo apruebe tiene que estar en `tagOwners` (admin). Después, en el panel, el nodo
figura como *Tagged devices* y ya no a nombre de un usuario.

## bench-master

Corre en la máquina del dev, escuchando en `127.0.0.1`. Como su proxy da consola serie y resets de todos los
benches, rechaza `Host` ajeno (DNS rebinding) y escrituras o WebSockets con `Origin` ajeno (CSRF desde
cualquier página abierta en el browser). Con `--host 0.0.0.0` lo ve toda la red: no hacerlo sin autenticación.

## Updates automáticos

Los benches sin PIN instalan solos el último tag `vX.Y.Z` del repo (boot + nocturno). Quien pueda pushear un tag
controla lo que corre en todos esos benches: cuidar quién tiene permiso de escritura en el repo. `POST /api/update`
deja mover un bench a cualquier rama/tag/commit **del repo** (no a otro origin): con `api_token` exige el token.

## Pendiente

- Identidad en la API de los benches vía `tailscale whois` (quién mandó qué por la consola), para auditoría.
- Token de escritura en la API (D13 del spec de agentes, `docs/specs/agents-cli.md` en `feat/agents`). El proxy
  de bench-master lo reenvía tal cual.
- No dejar los bins en el bench después del flash, y decodificar backtraces del lado del dev para que el `.elf`
  no esté en la SD.
- Ojo, no es solo la SD: si las placas no usan flash encryption en modo release, el firmware se lee directo de
  la flash del ESP32.

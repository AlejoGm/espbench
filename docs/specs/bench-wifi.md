# WiFi del banco (`espbench wifi`)

Estado: diseño acordado el 2026-11-27. Fases 1 y 3 hechas (con el bench simulado; falta la prueba en la sensibox con el dongle).

## Para qué

En el campo las placas usan WiFi (la red de configuración que trae el firmware) solo para la **configuración inicial**; después viven con
datos móviles. En la oficina la señal de datos es mala, así que el bench les da WiFi para que tengan conexión
mientras se prueban, y de paso permite probar el flujo de configuración inicial. No es para probar la
robustez del WiFi: alcanza con prenderla y apagarla.

## Decisiones

- **Red:** la de configuración que el firmware ya conoce (está en el firmware, repo privado), así las placas
  se conectan sin cargarles nada. **El SSID y la clave no van en este repo** (es público): se cargan desde el
  dashboard (WiFi → Configurar) o con `sudo espbench-wifi setup` en el bench, y quedan solo en el bench
  (`wifi.conf` y el keyfile de NetworkManager). La clave viaja solo hacia el bench: la API nunca la devuelve.
  Sin `api_token`, cualquiera en la red interna puede cambiarla (como las demás escrituras): conviene activarlo. Si cerca del banco ya existe una red con
  ese nombre, el setup avisa: las placas se podrían conectar a esa.
- **Apagada por defecto.** Al bootear el bench está apagada; solo se prende por comando.
- **Las placas salen a internet por el bench** (NAT por el cable del bench).
- **Hardware:** TP-Link TL-WN722N **v1** (Atheros AR9271, `0cf3:9271`, driver `ath9k_htc` en el kernel,
  modo AP). Solo 2,4 GHz, lo que usan los ESP32. Las v2/v3 (Realtek RTL8188EUS) no sirven para AP.

## Uso

```
espbench wifi [--bench B]                  # estado: apagada/prendida, SSID, canal, hasta cuándo, quién, placas conectadas
espbench wifi on [--for 8h]                # la prende (vence: default 8 h, máx 12 h)
espbench wifi off                          # la apaga (el estado normal)
```

`--json` y los exit codes de siempre. Un bench sin WiFi configurado: `unsupported`. Con varios benches y sin
`--bench` ni host: el único que tiene WiFi, o `ambiguous`.

Estado, con las placas por nombre (en el ESP32 la MAC de la estación es la MAC base, la que ya conoce
espbench):

```
WiFi: prendida · RedDelBanco · canal 6 · hasta 16:40 (facu)
  13875803451784  10.42.0.23  -41 dBm  conectada hace 12 min
```

## Cómo funciona

```
espbench (CLI) ──► GET/POST /api/wifi ──► benchwifi.py (estado, vencimiento, reservas, eventos)
                                               │ sudo -n
                                               ▼
                                  espbench-wifi (helper bash, como devremote)
                                  ├─ nmcli   hotspot de NetworkManager: DHCP + NAT por el cable
                                                                                                      └─ iw      estaciones conectadas (+ leases de DHCP de NM)
```

- **Helper** `/usr/local/bin/espbench-wifi` (root): subcomandos fijos y validados — `setup` (interactivo:
  interfaz, SSID, clave por teclado sin mostrarla, canal; escanea y avisa si el SSID ya está en el aire),
  `up`, `down`, `status --json`, `available`. Solo toca la interfaz del dongle, nunca la del cable. El
  api (usuario `sfypi`, que tiene `sudo` sin contraseña desde `install.sh`) lo llama con `sudo -n`.
- **Config** `/opt/esp/wifi.conf` (`IFACE`, `SSID`, `PASS`, `CHANNEL`), root:sfypi 640 como `api_token`. Sin
  el archivo, el bench no tiene WiFi. **La clave nunca sale por la API** ni va en un argumento.
- **Conexión de NM** `espbench-wifi`: hotspot (`802-11-wireless.mode ap`, `ipv4.method shared`, banda `bg`,
  canal fijo), `autoconnect no`: no se prende sola al bootear.
- **Estado** `meta/wifi_state.json`: `{on, until, by, since}`. El vencimiento lo hace un timer del api; si el
  api se reinicia, al arrancar relee el estado y apaga lo vencido. Al bootear: apagada (la conexión de NM no
  tiene autoconnect y el estado guardado vencido o no, se apaga si el helper dice que no está arriba).

## Reglas

1. **Prendida vence.** `on` dura 8 h por defecto (una jornada), máx 12 h. Nadie la deja prendida por olvido.
2. **Es un recurso compartido.** Prender la red cambia el comportamiento de las placas (en modo dual pasan
   del celular al WiFi). Si alguna placa del bench está reservada por otro, cualquier cambio da `locked`
   (exit 6) con quién, salvo `force` desde el dashboard (como las reservas).
3. **Todo queda en el log de cada placa.** Cada cambio es un evento `wifi` (`detail`: `action`, `until`,
   `user`) en cada placa del bench, con el cursor del fin de su log: en el monitor y el historial se ve en
   qué línea pasó. Las escrituras llevan `user` como las demás (`ESPBENCH_USER`).

## API

| Ruta | |
|---|---|
| `POST /api/wifi/config` `{ssid, password?, channel?, ...}` | SSID, clave (vacía = la que estaba) y canal. La clave va al helper por stdin, nunca vuelve. Con la red prendida, la regla de reservas |
| `GET /api/wifi` | `{available, configured, on, ssid, channel, until, by, since, clients: [{mac, ip, signal_dbm, connected_s, board}], error}`. Lectura abierta, como el resto; sin la clave |
| `POST /api/wifi` `{action: on\|off, for_s?, user?, lock_user?, lock_token?, force?}` | Cambia el estado. Token de la API si existe `api_token`. 423 `locked` si una placa la tiene reservada otro (salvo `force`); 409 `unsupported` sin WiFi; 400 `bad_request` (acción, duración). Devuelve el estado y `events`: `{board: cursor}` |

## Fases

| Fase | Qué | Necesita |
|---|---|---|
| 0 | Chequear el dongle en la sensibox (`lsusb` = `0cf3:9271`, `iw list` con `AP`) | la sensibox libre |
| 1 | Helper (`setup`, `up`, `down`, `status`) + `benchwifi.py` + `/api/wifi` + `espbench wifi`/`on`/`off` + eventos + reservas + vencimiento | — (bench simulado) |
| 2 | ~~`cut`, `offline`, `degrade`~~ descartada: el WiFi solo se usa para la configuración inicial, no hace falta probar su robustez | — |
| 3 | Dashboard: chip en el header del bench (apagada / N placas · hasta HH:MM) con menú (duración 1/4/8/12 h y Prender; placas conectadas, Extender 8 h y Apagar); bench-master lo muestra (solo lectura) | hecha |

Todo se programa y testea con el bench simulado (helper falso); la sensibox solo para la prueba final.

## Configurar desde el dashboard

Con un dongle USB enchufado y sin configurar (`configured: false`; una Pi sin dongle no ofrece nada: solo cuentan
interfaces WiFi en USB que no son la salida del bench), el chip dice "WiFi · sin configurar" y el menú es el
formulario: nombre, clave y canal. Configurada, "Configurar la red" lo abre con el nombre y el canal cargados y la
clave vacía (vacía = la que ya estaba). `espbench-wifi configure` recibe los datos por stdin.

---
target: remote/dashboard/device.html
total_score: 21
max_score: 40
na_heuristics: 
p0_count: 1
p1_count: 2
target_identity: "file:/Users/alegmata/Dev/espbench/remote/dashboard/device.html"
target_fingerprint: "sha256:4ef23a3fc194eefb2af0e9bb566ecc3706fbf5d9ebfaf9dc653b2594e2a53f14"
target_path: /Users/alegmata/Dev/espbench/remote/dashboard/device.html
timestamp: 2026-10-27T13-41-42Z
slug: remote-dashboard-device-html
---
Method: dual-agent (A: subagente de revisión de diseño · B: subagente de detector y navegador)

## Design Health Score

| # | Heurística | Nota | Problema principal |
|---|---|---|---|
| 1 | Visibilidad del estado | 2 | "en vivo" es el WebSocket, no la placa; un tty inexistente se ve "en vivo" con acciones activas |
| 2 | Relación con el mundo real | 3 | "Sesión" (reiniciar devremote) choca con la pestaña "Sesiones" |
| 3 | Control y libertad | 2 | Reset/Boot sin confirmación ni feedback; "Limpiar" sin deshacer |
| 4 | Consistencia | 2 | Panics de una corrida en rojo en Eventos y en el log; "Último panic" rojo aunque esté cubierto |
| 5 | Prevención de errores | 1 | Reservada por otro: los botones de escritura se ven igual; el aviso llega después, en confirm() |
| 6 | Reconocer antes que recordar | 2 | Atajos solo en tooltips; íconos ambiguos bajo 1280 px |
| 7 | Flexibilidad y eficiencia | 3 | Buenos atajos; falta saltar entre eventos y abrir un evento en el vivo |
| 8 | Estética y minimalismo | 2 | 6 badges de identidad con el peso de la reserva; cientos de send "" en Eventos |
| 9 | Diagnóstico de errores | 3 | Errores del API claros; log vacío sin explicación |
| 10 | Ayuda | 1 | Sin referencia de atajos ni de las marcas del margen |
| **Total** | | **21/40** | **Aceptable** |

## Veredicto de especificidad
Hecho para este producto: marcas de eventos en el margen del log, visor de rst a rst con la línea exacta, separadores y pestaña de tests, lectura de ESP-IDF. La cáscara (píldoras en todo, toolbar genérica, tema con el peso de Reset) es intercambiable y no llegó al rumbo "preciso y técnico".
Detector: 5 hallazgos en CLI (side-tab en .toast, pulsing-dot en .live, contraste de --bad y de placeholders con --ink3) y 11 de contraste en el navegador oscuro con el historial abierto (ink3 en badges, horas, contador; hora del log 2,8:1; --bad como texto chico; chip .ev-who 3,3:1). Falsos positivos: .live no se dibuja en device.html; el borde del toast es de estado.

## Problemas prioritarios
- [P0] Estado de conexión haciéndose pasar por estado de la placa → pill de EB.boardStatus junto al nombre, conexión aparte; 404 y log vacío con estado explícito y acciones deshabilitadas. harden, clarify.
- [P1] La reserva de otro no previene nada → franja de reserva bajo el header con Liberar/Forzar; candado visible en Reset/Boot/Sesión/Enviar; diálogo propio en vez de confirm(); rastro después de forzar. clarify, harden.
- [P1] Teclado y lector de pantalla: eventos y casos de test no enfocables, visor sin role=dialog ni foco, consola sin indicador de foco ni label, contraste de ink3 y de la hora del log. audit, harden.
- [P2] Ruido contra señal: plegar rachas de send, atenuar eventos dentro de una corrida, "Último panic" neutro si está cubierto. distill, quieter.
- [P2] Acciones del header ambiguas: renombrar "Sesión", bajar el tema a terciario, feedback de Reset/Boot, "Más" para lo raro. clarify, layout.

## Persona Red Flags
- Alex: sin atajos para Reset/Boot (el tooltip muestra los de idf.py monitor); el panel salta cada 10 s; Limpiar sin deshacer.
- Sam: no llega con teclado a eventos ni tests; el visor no atrapa el foco; marcas del margen solo en title; conexión solo por color en celular.
- Ingeniero depurando un crash: backtrace sin copiar ni decodificar; del visor no se va a la línea en el vivo; el header no dice "Panic hace 2 min"; en celular la hora se come un tercio del ancho.

## Minor Observations
Tooltip de tests "1 fallaron" (concordancia y número distinto del badge); badge de tests fuera de vista en el carrusel del celular; tcp/hw/sn con peso de primer nivel; confirm() rompe el tema oscuro; "⤓" sin label; "Sin flasheos" sin siguiente paso; eventos sin cursor atenuados sin explicación.

## Questions to Consider
- Si se entra al monitor a actuar, ¿por qué la primera fila es un inventario de identidad y no "qué le pasa y quién la tiene"?
- ¿Un panic durante espbench test tiene que verse igual que uno real?
- ¿Y si cada evento fuera un salto al byte del vivo en vez de un modal?

# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

El equipo de firmware y electrónica de Sensify: ingenieros que flashean y depuran placas ESP32 conectadas a un
banco remoto (una Raspberry Pi u otra máquina con las placas enchufadas). Usan el dashboard del bench y bench-master
mientras trabajan sobre una placa real.

Los agentes (Claude Code) usan el mismo banco por el CLI `espbench`, no por la UI. Lo que hacen (flasheos, envíos por
la consola, reservas, corridas de tests) aparece en el dashboard, donde lo ve el equipo.

## Product Purpose

Operar placas remotas como si estuvieran sobre el escritorio: elegir una placa, reservarla, flashearla, resetearla,
mandarle comandos por la consola serie, leer su log en vivo y ver qué le pasó (reinicios, panics, flasheos, corridas
de tests). Éxito: que el equipo pueda **actuar sobre una placa** sin ir al banco físico, y que el estado que ve sea
confiable para decidir qué hacer.

## Positioning

Un log serie persistente por placa, con eventos anclados a la línea exacta donde pasaron (cursor por byte), compartido
entre humanos y agentes. La misma placa, el mismo historial y las mismas reservas desde el dashboard, bench-master y el
CLI.

## Operating Context

- Varios bancos en distintas oficinas (la ubicación se detecta por IP o se fija a mano), alcanzables por la red
  interna o Tailscale. bench-master corre en la máquina del developer y junta todos los bancos.
- Ciclo de trabajo: build en la máquina del dev → flash remoto → verificar el arranque → comandos por la consola →
  leer la respuesta en el log → iterar. También corridas de tests de Unity (`espbench test`) que reinician la placa a
  propósito.
- Reservas con vencimiento y un lock que deja cada flash: varias personas y agentes comparten las mismas placas.
- Acciones sobre una placa en el dashboard: reservar y liberar (o forzar), reset, bootloader, reiniciar la sesión,
  consola serie, nota y propiedades, ACK de problemas, actualizar el bench.

## Capabilities and Constraints

- **Prioridad: que sea funcional.** Lo que el equipo necesita para operar la placa va antes que la apariencia.
- Hechos del código actual (ver `remote/dashboard/CLAUDE.md`), no decisiones nuevas:
  - Frontend HTML/CSS/JS sin framework ni build, servido por el api del bench (FastAPI). La lógica sin DOM vive en
    `remote/dashboard/espbench.js` con tests en node.
  - bench-master reusa `espbench.js` y `style.css` del bench: la card de placa es la misma en los dos.
  - Todas las URLs son relativas (bench-master sirve el frontend del bench por su proxy, `/bench/<n>/`).
  - La interfaz está en español.
  - Escrituras con token opcional de la API; las lecturas y el log en vivo quedan abiertos en la red interna.
- Terminología: placa, bench (banco), sesión de log, evento, cursor, reserva, lock del flash, corrida de tests, ACK.
- Abierto: no se definió si el uso desde el celular, el funcionamiento sin internet o el soporte de navegadores son
  requisitos; hoy el código cuida el celular (áreas táctiles, sin scroll horizontal) y funciona sin las fuentes e
  íconos del CDN.

## Evidence on Hand

- Documentación del sistema: `README.md`, `docs/ARCHITECTURE.md`, los `CLAUDE.md` de cada carpeta.
- Placa simulada para probar la UI sin hardware: `tests/benchsim.py`.
- No hay métricas de uso, usuarios externos ni casos publicados: no inventarlos.

## Product Principles

1. **Operar primero.** Cada pantalla deja actuar sobre la placa (reservar, resetear, mandar, flashear) sin rodeos;
   lo que no ayuda a operar, va después.
2. **Estado confiable.** Rojo significa un problema real de la placa; lo esperado (los reinicios de un test, un
   problema ya visto con ACK) no alarma.
3. **Del síntoma a la línea.** Todo evento lleva al punto exacto del log donde pasó.
4. **Un solo banco, muchas vías.** Dashboard, bench-master y CLI muestran la misma verdad: reservas, eventos y
   corridas hechas por un agente se ven igual que las de una persona.

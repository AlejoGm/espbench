---
name: espbench
description: Tablero de los bancos de placas ESP32 remotas — estado, log en vivo y acciones por placa.
colors:
  page: "#EEF2F7"
  card: "#FFFFFF"
  row: "#F4F6FA"
  line: "#E3E8EF"
  line-strong: "#D3DAE4"
  ink: "#18212C"
  ink-muted: "#5B6676"
  ink-faint: "#9AA4B2"
  accent: "#FF8A1F"
  accent-ink: "#4A2400"
  ok: "#1FB57A"
  ok-bg: "#DDF5EA"
  bad: "#E5484D"
  bad-bg: "#FCE3E5"
  warn: "#E39A2D"
  warn-bg: "#FDF0DB"
  flash: "#7B6CF6"
  flash-bg: "#ECE9FE"
  test: "#0E9AB0"
  test-bg: "#DDF3F7"
  boot: "#B8C2CF"
  page-dark: "#0F1122"
  card-dark: "#181B30"
  row-dark: "#20243C"
  ink-dark: "#EEF0FA"
  terminal-bg: "#0E1117"
  terminal-ink: "#C9D1D9"
typography:
  headline:
    fontFamily: "Manrope, system-ui, -apple-system, sans-serif"
    fontSize: "26px"
    fontWeight: 700
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Manrope, system-ui, -apple-system, sans-serif"
    fontSize: "18px"
    fontWeight: 700
    letterSpacing: "-0.01em"
  board-name:
    fontFamily: "Manrope, system-ui, -apple-system, sans-serif"
    fontSize: "16px"
    fontWeight: 800
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Manrope, system-ui, -apple-system, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.45
    fontFeature: "tnum"
  label:
    fontFamily: "Manrope, system-ui, -apple-system, sans-serif"
    fontSize: "12.5px"
    fontWeight: 600
  log:
    fontFamily: "JetBrains Mono, ui-monospace, Menlo, Consolas, monospace"
    fontSize: "12.5px"
    fontWeight: 400
    lineHeight: 1.55
rounded:
  pill: "999px"
  lg: "24px"
  md: "16px"
  sm: "10px"
  spec: "8px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "20px"
  card: "24px"
  page: "28px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.page}"
    rounded: "{rounded.pill}"
    height: "38px"
    padding: "0 16px"
  button-accent:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    rounded: "{rounded.pill}"
    height: "44px"
    padding: "0 18px"
  button-action:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    height: "34px"
    padding: "0 14px"
  button-icon:
    backgroundColor: "{colors.row}"
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.pill}"
    size: "34px"
  status-ok:
    backgroundColor: "{colors.ok-bg}"
    textColor: "{colors.ok}"
    rounded: "{rounded.pill}"
    padding: "4px 10px"
  status-bad:
    backgroundColor: "{colors.bad-bg}"
    textColor: "{colors.bad}"
    rounded: "{rounded.pill}"
    padding: "4px 10px"
  fact:
    backgroundColor: "{colors.row}"
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.pill}"
    padding: "6px 11px"
  prop-spec:
    backgroundColor: "{colors.row}"
    textColor: "{colors.ink}"
    rounded: "{rounded.spec}"
    height: "28px"
  board-card:
    backgroundColor: "{colors.card}"
    rounded: "{rounded.lg}"
    padding: "{spacing.card}"
  terminal:
    backgroundColor: "{colors.terminal-bg}"
    textColor: "{colors.terminal-ink}"
    typography: "{typography.log}"
---

# Design System: espbench

## Overview

**Creative North Star: "El tablero del banco"**

El dashboard es el tablero de instrumentos de un banco de laboratorio. Cada placa es un instrumento con su lectura en
una palabra ("En línea", "Panic", "Testeando"), sus contadores en píldoras y una tira de barras de las últimas 24 h; el
log serie es la pantalla del instrumento, oscura en los dos temas. Todo está pensado para leerse de un vistazo y para
actuar sin buscar: la acción sobre la placa (Monitor, Reset, Enviar) siempre está a la vista.

Hoy el sistema es blando: superficies tonales sobre un fondo azulado, casi sin bordes ni sombras, todo redondeado
(píldoras para botones, estados y chips; cards de 24 px). La densidad es media en la home (grilla de cards) y alta en el
monitor (header compacto de chips, toolbar de íconos, log a pantalla completa).

**Rumbo confirmado: más preciso y técnico.** Los componentes nuevos o retocados van hacia menos redondeo, bordes finos
y más densidad, más cerca de una herramienta de ingeniería. Es una dirección, no un cambio ya hecho: los tokens de
arriba describen el sistema actual y se ajustan en el próximo `/impeccable document` cuando el cambio exista en código.

**Key Characteristics:**
- El color es estado: verde sana, rojo problema, ámbar advertencia o reserva, violeta flash, turquesa tests.
- Superficies tonales (page → card → row) en lugar de bordes y sombras.
- Píldoras para casi todo lo que se toca o se lee como dato.
- Log serie en una pantalla oscura con fuente mono, igual en tema claro y oscuro.
- Tema claro y oscuro con las mismas variables; las fuentes y los íconos del CDN son opcionales.

## Colors

Una base neutra fría (gris azulado) y una paleta de estados saturada, donde cada color nombra un tipo de cosa que le
pasa a una placa.

### Primary
- **Naranja de acción** (`accent`): la única acción destacada de una pantalla (Actualizar el bench), el interruptor
  encendido y el foco de los campos. Su texto es el marrón oscuro `accent-ink`.

### Neutral
- **Papel azulado** (`page`): fondo de página.
- **Blanco card** (`card`): superficie de cards, header del monitor, panel lateral.
- **Fila** (`row`): tercer nivel tonal; fondo de píldoras de datos, botones de ícono, bloques internos de la card.
- **Línea** y **línea fuerte** (`line`, `line-strong`): bordes de botones de acción y separadores; casi nunca se usan.
- **Tinta**, **tinta media**, **tinta tenue** (`ink`, `ink-muted`, `ink-faint`): texto principal, secundario y
  metadatos (horas, ejes, etiquetas de badge).
- Tema oscuro: los mismos roles en azul noche (`page-dark`, `card-dark`, `row-dark`, `ink-dark`).

### Estados (el corazón del sistema)
- **Verde sana** (`ok` / `ok-bg`): En línea, flasheo exitoso, tests que pasaron, el punto "vivo".
- **Rojo problema** (`bad` / `bad-bg`): panic, boot loop, placa caída, test fallado, estado "no tocar / roto".
- **Ámbar advertencia** (`warn` / `warn-bg`): reset anormal, sin log, reserva o lock, nota, ubicación vieja.
- **Violeta flash** (`flash` / `flash-bg`): flasheando, borrando, iniciando; también los envíos por la consola.
- **Turquesa tests** (`test` / `test-bg`): corrida de `espbench test` en curso.
- **Gris reinicio** (`boot`): reinicios en las barras de actividad.

### Named Rules
**The State-Is-Color Rule.** Cada color de estado nombra una sola clase de evento de la placa y aparece igual en el
estado de la card, sus barras, los chips del header y los eventos del historial. Un color nuevo solo entra con un estado
nuevo.

**The Tinted-Pair Rule.** Un estado se dibuja con su par: texto en el color pleno sobre su fondo `-bg`. El color pleno
de fondo (blanco encima) queda para lo más grave: boot loop en curso y "no tocar / roto".

## Typography

**Body Font:** Manrope (con system-ui de respaldo)
**Mono Font:** JetBrains Mono (con ui-monospace, Menlo, Consolas)

**Character:** Manrope geométrica y compacta para la interfaz, con números tabulares en todo el body para que los
contadores no bailen; JetBrains Mono solo para el log, los IDs (MAC, job id, sesión) y algún chip técnico.

### Hierarchy
- **Headline** (700, 26px, -0.02em): nombre del bench en la home.
- **Title** (700, 18–20px, -0.01em): títulos de sección y de card.
- **Board name** (800, 16px, -0.01em): nombre de la placa en la card y en el header del monitor.
- **Body** (400, 14px, 1.45): texto general.
- **Label** (600–700, 11.5–12.5px): píldoras, estados, badges, facts, chips de eventos.
- **Log** (400, 12.5px, 1.55; 11.5px en celular): líneas del log serie, con la hora en tinta tenue.

### Named Rules
**The Tabular Rule.** Todo número que se actualiza en vivo (contadores, tiempos, progreso) va en cifras tabulares.

## Layout

La home es una columna centrada (máx. 1320px, 28px de margen, 20px entre bloques): header del bench, pestañas con
búsqueda y agrupación, y una grilla de cards de placa. El monitor ocupa toda la ventana (`100dvh`): header compacto, una
toolbar de una línea, el log a todo lo que queda y, a la derecha, el panel de historial de 360px que se superpone en
celular. La consola serie es una barra fija abajo.

Ritmo de espaciado en múltiplos de 4: 4–8px entre píldoras, 16–20px entre bloques, 24px de padding de card.

Puntos de corte: 1279px (las acciones y la toolbar del monitor pasan a solo íconos), 760px (una columna, sin leyenda,
chips del header en una línea deslizable, panel lateral a pantalla completa). Con puntero grueso los chips y botones
chicos crecen a 32px de área táctil.

## Elevation & Depth

Plano por defecto. La profundidad sale de tres niveles tonales (page, card, row), no de sombras. La única sombra es una
línea de 1px casi invisible (`0 1px 2px rgba(24,33,44,.04)`) en cards y píldoras flotantes del tema claro; en el oscuro
no hay. Un borde interior suave del color de estado marca cards especiales (no tocar, testeando).

### Named Rules
**The Tonal-Depth Rule.** Para separar algo, bajar o subir un nivel tonal antes que agregar borde o sombra.

## Shapes

Redondeo generoso y consistente: píldora completa (999px) para botones, estados, chips, pestañas, búsqueda y badges;
24px para cards; 16px para el header del monitor y el panel lateral; 10px para ítems de listas y bloques internos.
La excepción son las propiedades "spec" de la placa (chip, conectividad): cuadradas de 8px, para distinguir qué es la
placa de para qué se usa. Los bordes punteados significan "agregar" o "fuera del catálogo".

## Components

### Buttons
- **Shape:** píldora (999px).
- **Primary:** fondo tinta con texto del color de página, 38px de alto (Monitor).
- **Accent:** naranja con texto marrón, 44px (Actualizar el bench); uno por pantalla.
- **Action:** blanco con borde fino, 34px (30px en el header del monitor); el borde se oscurece al pasar el mouse.
  Variante peligrosa: texto rojo, fondo rojo tenue al pasar (Sesión, Forzar).
- **Icon:** círculo de 34px sobre fila, ícono en tinta media.
- **Focus:** contorno naranja de 2px separado 2px.

### Chips
- **Estado** (`.status`): píldora con punto de color y una palabra; el par tonal del estado.
- **Facts y badges:** píldoras en fila con etiqueta en tinta tenue y valor en negrita.
- **Propiedades:** specs cuadradas y neutras (el chip invertido en tinta), etiquetas redondas, estado ámbar, estado que
  excluye en rojo lleno.
- **Eventos:** píldoras de filtro con conteo; activa en tinta invertida.

### Cards / Containers
- **Corner Style:** 24px.
- **Background:** blanco card sobre papel azulado.
- **Shadow Strategy:** ver Elevation; línea casi invisible.
- **Internal Padding:** 24px; bloques internos (tests, facts) sobre fondo fila con 10px de radio.

### Inputs / Fields
- **Style:** píldora sin borde, fondo card o fila, ícono a la izquierda en tinta tenue.
- **Focus:** borde interior naranja de 1.5px.

### Navigation
- Pestañas en píldora: la activa sube a card con sombra; el conteo en negrita tenue al lado.
- En el monitor, "← Placas" vuelve a la home (o al master) con el filtro que tenía.

### Board Card (signature)
El instrumento: nombre y estado arriba, propiedades y nota, facts (encendida, último log, reinicios, panics), el bloque
de tests cuando hay una corrida, barras de actividad de 24 h y el pie con firmware, lock y Monitor. Es la misma en el
bench y en bench-master.

### Terminal (signature)
El log serie en pantalla oscura con JetBrains Mono, la hora a la izquierda en tinta tenue, marcas de eventos en el
margen (⚠ panic, ↻ boot, › send, ⚡ flash, ▶ inicio de test) y líneas de panic resaltadas en rojo.

## Do's and Don'ts

### Do:
- **Do** usar los colores de estado solo para estados de la placa, siempre con su par tonal.
- **Do** separar con niveles tonales (page, card, row) antes que con bordes o sombras.
- **Do** mantener el log oscuro y en mono en los dos temas.
- **Do** dar a cada número en vivo cifras tabulares y una etiqueta que diga qué cuenta.
- **Do** llevar los componentes nuevos hacia el rumbo confirmado: más precisos y técnicos (menos redondeo, bordes
  finos, más densidad), sin mezclar dos estilos en una misma pieza.

### Don't:
- **Don't** usar un color de estado como decoración: si algo es rojo, la placa tiene un problema.
- **Don't** depender de las fuentes o íconos del CDN: sin red tiene que leerse igual con las del sistema.
- **Don't** usar más de una acción naranja por pantalla.

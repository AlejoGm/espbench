#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
logrange.py — rangos del log de una placa: anchors, --until, --around, filtros.

Lo usa api.py en GET /api/board/{key}/log y /events (docs/specs/agents-cli.md
§5 y §7.3). Solo lee: output.log (sesión actual), output_<sesión>.log (las
anteriores) y events.jsonl, todos en el directorio de la placa
(devices/<MAC>/). Es puro salvo por el disco: la hora llega por parámetro.

Anchors (`since`, `around`):
- now / session
- tipo de evento con ordinal: boot, panic~1 ... (en la sesión actual, por offset)
- tiempo: 5m, 30s, 2h, 16:02, 16:02:03, 2026-10-05T16:02 (zona de la Pi)
- cursor c:<sesión>:<offset> (si cae a mitad de línea, al inicio de esa línea)

`until`: el primer X después de since, sin cruzar la sesión. X es un tipo de
evento o un patrón ("re:<regex>" o substring). Los patrones se evalúan sobre el
texto sin prefijo ni ANSI, después del \\r, y sobre la línea lógica (una línea
`>` más sus `↪`, aunque haya líneas taglog en el medio).

`boot` y `panic` como until se buscan en las líneas (misma detección que
SerialWatch), no en events.jsonl: el evento se escribe un instante después de
la línea, y un poll que viera la línea sin el evento seguiría desde un `end`
posterior y no lo encontraría nunca. Los demás tipos salen de events.jsonl,
leído DESPUÉS de fijar el tamaño del log: un evento que todavía no estaba
tiene cursor >= ese tamaño, y lo encuentra el próximo poll.

events.jsonl no tiene tope (sin rotación, §4): se lee UNA vez por pedido
(_Events) y, para la sesión actual, de atrás para adelante hasta pasar el
rango pedido (sus eventos son los últimos del archivo). Los últimos N de
/events, igual. El conteo por tipo de todo el archivo (`counts`) se lleva de
forma incremental (el archivo solo crece).
"""
import collections
import datetime as dt
import os
import pathlib
import re
import threading
import time
from typing import Optional

from server import events
from server.serial_watch import line_kind

try:                        # regex: el mismo dialecto que re, con timeout (remote/requirements.txt)
    import regex as _regex
except ImportError:         # pragma: no cover - depende de lo instalado
    _regex = None

DEFAULT_MAX_LINES = 200
MAX_MAX_LINES = 5000
HEAD_LINES = 50
SLACK = 2.0              # s; > MAX_HOLD de DeviceLog: las horas del archivo no son monótonas
LINE_TYPES = ("boot", "panic")   # until que se buscan en las líneas
# Leyendo events.jsonl hacia atrás: eventos seguidos fuera de lo pedido antes de
# dejar de leer. Device y api escriben en paralelo y el orden del archivo no es
# exactamente el del log; el desorden es de unos pocos eventos.
EVENT_SLACK = 64

_PREFIX_RE = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d\.\d{3}) ([>|↪]) ")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROGRESS_RE = re.compile(r"\b(?:Writing|Reading) at 0x[0-9a-fA-F]+")
_EVENT_RE = re.compile(r"([a-z_]+)(?:~(\d+))?")
_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)(s|m|h|d)")
_CLOCK_RE = re.compile(r"(\d{1,2}):(\d\d)(?::(\d\d)(?:\.(\d{1,6}))?)?")
_ISO_RE = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d{1,6})?)?(?:Z|[+-]\d\d:?\d\d)?")
_DUR_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Patrones del usuario (grep, until=re:) sin auth: tope de largo, de texto
# evaluado y de tiempo. Con `regex`, timeout por búsqueda; sin él (fallback),
# se rechazan los cuantificadores anidados y las alternancias cuantificadas
# ((a|a)+ explota igual que (a+)+ con `re`).
MAX_PATTERN = 256
MAX_EVAL_CHARS = 4096
REGEX_TIMEOUT = 0.1         # s por búsqueda (solo con `regex`)
REGEX_BUDGET = 2.0          # s en total por pedido
_NESTED_QUANT_RE = re.compile(r"\((?:[^()\\]|\\.)*(?:[+*]|\{\d*,?\d*\})(?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,?\d*\})")
_ALT_QUANT_RE = re.compile(r"\((?:[^()\\]|\\.)*\|(?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,?\d*\})")


class RangeError(Exception):
    """error: bad_anchor | cursor_expired | not_found | bad_request (el contrato
    del CLI, §8.3)."""

    def __init__(self, error: str, message: str):
        super().__init__(message)
        self.error = error
        self.message = message


# ---------- líneas ----------

class Line:
    __slots__ = ("offset", "end", "date", "time", "origin", "body", "_clean")

    def __init__(self, offset: int, raw: bytes):
        self.offset = offset
        self.end = offset + len(raw) + 1
        text = raw.decode("utf-8", errors="replace")
        m = _PREFIX_RE.match(text)
        if m:
            self.date, self.time, self.origin = m.group(1), m.group(2), m.group(3)
            self.body = text[m.end():]
        else:
            self.date = self.time = self.origin = None
            self.body = text
        self._clean = None

    @property
    def serial(self) -> bool:
        return self.origin in (">", "↪", None)

    def clean(self) -> str:
        if self._clean is None:
            self._clean = clean_text(self.body)
        return self._clean

    def ts(self) -> Optional[float]:
        if self.date is None:
            return None
        return dt.datetime.strptime(f"{self.date} {self.time}", "%Y-%m-%d %H:%M:%S.%f").timestamp()

    def render(self, date: Optional[str], raw: bool = False) -> str:
        body = self.body if raw else self.clean()
        if self.date is None:
            return body
        stamp = self.time if self.date == date else f"{self.date} {self.time}"
        return f"{stamp} {self.origin} {body}"


def clean_text(body: str) -> str:
    """Sin ANSI y con el \\r aplicado (queda el último segmento), como SerialWatch."""
    return _ANSI_RE.sub("", body.rstrip("\r").rsplit("\r", 1)[-1])


def _forward(f, start: int, stop: int):
    """Líneas completas en [start, stop)."""
    f.seek(start)
    pos = start
    for raw in f:
        if pos >= stop or not raw.endswith(b"\n"):
            return
        yield Line(pos, raw[:-1])
        pos += len(raw)


def _backward(f, end: int, chunk: int = 65536):
    """Líneas completas que terminan en o antes de `end` (alineado), de la
    última a la primera."""
    buf, pos = b"", end
    while True:
        i = buf.rfind(b"\n", 0, max(0, len(buf) - 1))
        if i >= 0:
            yield Line(pos + i + 1, buf[i + 1:-1])
            buf = buf[:i + 1]
            continue
        if pos == 0:
            if buf:
                yield Line(0, buf[:-1])
            return
        start = max(0, pos - chunk)
        f.seek(start)
        buf = f.read(pos - start) + buf
        pos = start


def _align_end(f, size: int) -> int:
    """Fin de la última línea completa (lo que haya después de el último \\n no
    es una línea todavía)."""
    pos = size
    while pos > 0:
        start = max(0, pos - 8192)
        f.seek(start)
        i = f.read(pos - start).rfind(b"\n")
        if i >= 0:
            return start + i + 1
        pos = start
    return 0


def _align_start(f, offset: int) -> int:
    """Inicio de la línea que contiene `offset`."""
    pos = offset
    while pos > 0:
        start = max(0, pos - 8192)
        f.seek(start)
        i = f.read(pos - start).rfind(b"\n")
        if i >= 0:
            return start + i + 1
        pos = start
    return 0


# ---------- archivos de la placa ----------

class _Session:
    """Un archivo de sesión abierto, con el tamaño fijado al abrirlo."""

    def __init__(self, sid: str, path: pathlib.Path, f, size: int):
        self.sid, self.path, self.f, self.size = sid, path, f, size

    def cursor(self, offset: int) -> str:
        return events.format_cursor(self.sid, offset)


class BoardLog:
    """Archivos de log y eventos de una placa (devices/<MAC>/)."""

    def __init__(self, home: pathlib.Path):
        self.home = pathlib.Path(home)
        self.current_path = self.home / "output.log"
        self.events_path = self.home / "events.jsonl"
        self._open = []

    def close(self) -> None:
        for f in self._open:
            f.close()
        self._open.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def current_session(self) -> Optional[str]:
        return events.read_session_id(self.current_path)

    def open_session(self, sid: Optional[str] = None) -> _Session:
        """Abre la sesión `sid` (None = la actual) y verifica el header en el
        mismo fd: si output.log rotó en el medio, se busca la rotada."""
        candidates = [self.current_path]
        if sid is not None:
            candidates += [self.home / f"output_{sid}.log"] + sorted(self.home.glob(f"output_{sid}_*.log"))
        for path in candidates:
            try:
                f = open(path, "rb")
            except OSError:
                continue
            found = events.read_session_id_from(f)
            if found is not None and (sid is None or found == sid):
                self._open.append(f)
                return _Session(found, path, f, _align_end(f, f.seek(0, os.SEEK_END)))
            f.close()
        if sid is None:
            raise RangeError("not_found", "la placa no tiene log con sesión")
        raise RangeError("cursor_expired", f"la sesión {sid} ya no está en la Pi")

    def read_events(self) -> list:
        return events.read(self.events_path)


def _event_key(ev: dict):
    cur = events.parse_cursor(ev.get("cursor"))
    return cur if cur else ("", 0)


class _Events:
    """events.jsonl leído una sola vez por pedido, y DESPUÉS de fijar el tamaño
    del log (lo pide el primero que lo necesita, que ya abrió la sesión).

    session(sid, lo): eventos de la sesión `sid` con offset >= lo, ordenados.
    De la sesión actual se lee hacia atrás hasta EVENT_SLACK eventos seguidos
    que no son del rango; de otra, el archivo entero (pedidos raros: un cursor
    de una sesión anterior). Un pedido con un `lo` mayor sale de lo ya leído."""

    def __init__(self, board: "BoardLog", current: Optional[str]):
        self.board, self.current = board, current
        self._key = None              # (sid, lo) de lo leído
        self._evs: list = []
        self.reads = 0

    def session(self, sid: str, lo: int = 0) -> list:
        if self._key is None or self._key[0] != sid or self._key[1] > lo:
            self.reads += 1
            if sid == self.current:
                evs = _session_tail(self.board.events_path, sid, lo)
            else:
                evs = [e for e in self.board.read_events() if _event_key(e)[0] == sid and _event_key(e)[1] >= lo]
            self._key, self._evs = (sid, lo), sort_events(evs)
        return [e for e in self._evs if _event_key(e)[1] >= lo]


def _session_tail(path, sid: str, lo: int, wanted: Optional[list] = None) -> list:
    """Eventos de la sesión `sid` con offset >= lo (de los tipos `wanted`),
    leyendo hacia atrás. El cursor se saca de los bytes (events.encode escribe
    `"cursor":"c:…"` antes de `detail`): las líneas de otras sesiones o de otros
    tipos no se parsean (con o sin espacios: un archivo escrito a mano también)."""
    cur_rx = re.compile(rb'"cursor":\s*"c:' + re.escape(sid.encode()) + rb':(\d+)"')
    keep = _type_filter(wanted or [])
    out, miss = [], 0
    for raw in events.read_back(path):
        m = cur_rx.search(raw)
        if m is None or int(m.group(1)) < lo:
            miss += 1
            if miss >= EVENT_SLACK:
                break
            continue
        miss = 0
        ev = events.parse_line(raw) if keep(raw) else None
        if ev is not None and _event_key(ev) == (sid, int(m.group(1))) and (not wanted or ev.get("type") in wanted):
            out.append(ev)
    return out


def _type_filter(wanted: list):
    """Filtro previo sobre los bytes (events.encode escribe `"type":"x"` sin
    espacios): las líneas de otros tipos no se parsean."""
    if not wanted:
        return lambda raw: True
    tags = [f'"type":"{t}"'.encode() for t in wanted]
    return lambda raw: b'"type":"' not in raw or any(t in raw for t in tags)


def _last_events(path, wanted: list, limit: int):
    """Los últimos `limit` eventos (de los tipos `wanted`) leyendo hacia atrás →
    (ordenados, more). Sigue hasta tener limit+1 (para saber si hay más) y
    EVENT_SLACK líneas de margen por el desorden."""
    keep = _type_filter(wanted)
    out, countdown = [], None
    for raw in events.read_back(path):
        if countdown is not None:
            countdown -= 1
            if countdown < 0:
                break
        ev = events.parse_line(raw) if keep(raw) else None
        if ev is None or (wanted and ev.get("type") not in wanted):
            continue
        out.append(ev)
        if countdown is None and len(out) > limit:
            countdown = EVENT_SLACK
    out = sort_events(out)
    return out[-limit:], len(out) > limit


_counts_lock = threading.Lock()
_counts_cache: dict = {}        # ruta → (inode, bytes contados, {tipo: n})


def all_counts(path) -> dict:
    """{tipo: n} de todo events.jsonl, incremental: el archivo solo crece (append),
    así que cada pedido cuenta solo lo nuevo. Un inode distinto o un archivo más
    chico (migración unknown → MAC, borrado a mano) arranca de cero."""
    try:
        st = os.stat(path)
    except OSError:
        return {}
    with _counts_lock:
        ino, done, counts = _counts_cache.get(str(path), (None, 0, {}))
        if ino != st.st_ino or st.st_size < done:
            done, counts = 0, {}
        if st.st_size > done:
            with open(path, "rb") as f:
                f.seek(done)
                data = f.read(st.st_size - done)
            cut = data.rfind(b"\n") + 1           # una línea a medio escribir, en el próximo
            for raw in data[:cut].splitlines():
                ev = events.parse_line(raw)
                if ev is not None:
                    counts[ev.get("type")] = counts.get(ev.get("type"), 0) + 1
            done += cut
        _counts_cache[str(path)] = (st.st_ino, done, counts)
        return dict(counts)


def sort_events(evs: list) -> list:
    """Por (sesión, offset): device y api escriben en paralelo y el orden del
    archivo no es el del log."""
    return sorted(evs, key=_event_key)


def compact_event(ev: dict) -> dict:
    out = {"ts": ev.get("ts"), "type": ev.get("type"), "cursor": ev.get("cursor")}
    if ev.get("detail"):
        out["detail"] = ev["detail"]
    return out


# ---------- tiempo ----------

def parse_time(anchor: str, now: float) -> Optional[float]:
    """5m / 16:02 / 2026-10-05T16:02 → epoch; None si no es un anchor de tiempo.
    Una hora sin fecha posterior a ahora (más de 1 min) es de ayer."""
    m = _DUR_RE.fullmatch(anchor)
    if m:
        return now - float(m.group(1)) * _DUR_UNITS[m.group(2)]
    m = _CLOCK_RE.fullmatch(anchor)
    if m:
        h, mi, s, frac = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), m.group(4) or "0"
        if h > 23 or mi > 59 or s > 59:
            raise RangeError("bad_anchor", f"hora inválida: {anchor}")
        base = dt.datetime.fromtimestamp(now)
        t = base.replace(hour=h, minute=mi, second=s, microsecond=int(frac.ljust(6, "0")))
        if t.timestamp() > now + 60:
            t -= dt.timedelta(days=1)
        return t.timestamp()
    if _ISO_RE.fullmatch(anchor):
        text = anchor.replace(" ", "T").replace("Z", "+00:00")
        if re.search(r"[+-]\d{4}$", text):
            text = text[:-2] + ":" + text[-2:]
        # fromisoformat de 3.9 solo acepta 3 o 6 dígitos de fracción
        text = re.sub(r"\.(\d{1,6})", lambda m: "." + m.group(1).ljust(6, "0"), text, count=1)
        try:
            d = dt.datetime.fromisoformat(text)
        except ValueError:
            raise RangeError("bad_anchor", f"fecha inválida: {anchor}")
        return d.timestamp()      # naive = hora local de la Pi
    return None


def _first_at_or_after(sess: _Session, t: float) -> int:
    """Primera línea con ts >= t (§3.6): hacia atrás hasta una línea con
    ts < t - SLACK (o el inicio de la sesión) y desde ahí hacia adelante. Las
    líneas sin hora no cuentan. Ninguna: el final."""
    stop = 0
    for line in _backward(sess.f, sess.size):
        ts = line.ts()
        if ts is not None and ts < t - SLACK:
            stop = line.offset
            break
    for line in _forward(sess.f, stop, sess.size):
        ts = line.ts()
        if ts is not None and ts >= t:
            return line.offset
    return sess.size


# ---------- anchors ----------

class Point:
    """Un punto del log: sesión + offset (inicio de línea). `etype`: el tipo de
    evento si el anchor era un evento (para no matchear el mismo en until)."""

    def __init__(self, sess: _Session, offset: int, etype: Optional[str] = None):
        self.sess, self.offset, self.etype = sess, offset, etype

    @property
    def cursor(self) -> str:
        return self.sess.cursor(self.offset)


def resolve(board: BoardLog, anchor: str, now: float, evs: Optional[_Events] = None) -> Point:
    anchor = (anchor or "").strip()
    if not anchor:
        raise RangeError("bad_anchor", "anchor vacío")
    cur = events.parse_cursor(anchor)
    if cur is not None:
        sess = board.open_session(cur[0])
        if cur[1] > sess.size:
            if cur[1] > sess.f.seek(0, os.SEEK_END):
                raise RangeError("bad_anchor", f"offset fuera del log: {anchor}")
            return Point(sess, sess.size)           # una línea que se está escribiendo
        return Point(sess, _align_start(sess.f, cur[1]))
    if anchor.startswith("c:"):
        raise RangeError("bad_anchor", f"cursor inválido: {anchor}")
    sess = board.open_session()
    if anchor == "now":
        return Point(sess, sess.size)
    if anchor == "session":
        return Point(sess, 0)
    m = _EVENT_RE.fullmatch(anchor)
    if m and m.group(1) in events.TYPES:
        etype, n = m.group(1), int(m.group(2) or 0)
        evs = evs if evs is not None else _Events(board, board.current_session())
        mine = [e for e in evs.session(sess.sid) if e.get("type") == etype and _event_key(e)[1] <= sess.size]
        if n >= len(mine):
            raise RangeError("bad_anchor", f"no hay {anchor} en la sesión actual ({len(mine)} {etype})")
        return Point(sess, _align_start(sess.f, _event_key(mine[-1 - n])[1]), etype)
    t = parse_time(anchor, now)
    if t is not None:
        return Point(sess, _first_at_or_after(sess, t))
    raise RangeError("bad_anchor", f"anchor desconocido: {anchor}")


# ---------- until ----------

class Pattern:
    """Patrón del usuario con topes (ReDoS: grep y until llegan sin auth).
    literal=True: substring (sin regex)."""

    def __init__(self, pattern: str, what: str, literal: bool = False):
        if len(pattern) > MAX_PATTERN:
            raise RangeError("bad_request", f"{what}: patrón de más de {MAX_PATTERN} caracteres")
        self.what = what
        self.literal = pattern if literal else None
        self.spent = 0.0
        if literal:
            return
        if _regex is None and (_NESTED_QUANT_RE.search(pattern) or _ALT_QUANT_RE.search(pattern)):
            raise RangeError("bad_request", f"{what}: cuantificadores anidados o alternancias cuantificadas "
                                            "no permitidos (p. ej. (a+)+, (a|b)+)")
        try:
            self.rx = _regex.compile(pattern) if _regex is not None else re.compile(pattern)
        except (re.error, getattr(_regex, "error", re.error)) as e:
            raise RangeError("bad_request", f"{what}: regex inválida: {e}")

    def search(self, text: str) -> bool:
        text = text[:MAX_EVAL_CHARS]
        if self.literal is not None:
            return self.literal in text
        t0 = time.monotonic()
        try:
            found = (self.rx.search(text, timeout=REGEX_TIMEOUT) if _regex is not None
                     else self.rx.search(text)) is not None
        except TimeoutError:
            raise self._too_slow()
        self.spent += time.monotonic() - t0
        if self.spent > REGEX_BUDGET:
            raise self._too_slow()
        return found

    def _too_slow(self) -> RangeError:
        return RangeError("bad_request", f"{self.what}: la regex tarda demasiado (simplificala o acotá el rango)")


def parse_until(until: str):
    """→ ("event", tipo) | ("line", tipo) | ("pattern", Pattern)."""
    if until in LINE_TYPES:
        return ("line", until)
    if until in events.TYPES:
        return ("event", until)
    if until.startswith("re:"):
        return ("pattern", Pattern(until[3:], "until"))
    return ("pattern", Pattern(until, "until", literal=True))


class _LogicalMatcher:
    """Evalúa patrones / tipos de línea sobre líneas lógicas: un `>` más sus
    `↪` (con taglog intercalado). feed() devuelve (línea `>` donde empieza,
    texto lógico) cuando matchea.

    Una línea lógica puede quedar partida entre dos polls (el poll 1 vio
    `> result=`, el `↪ OK` llega después) o la respuesta de un send puede ser
    la continuación de un prompt anterior al send: seed() arranca con la línea
    lógica abierta en `start` (el `>` anterior y sus `↪`), ya evaluada."""

    SEED_LINES = 200          # cuánto se mira hacia atrás buscando el `>` abierto

    def __init__(self, kind: str, what, skip_at: Optional[int], echo: Optional[str]):
        self.kind, self.what, self.skip_at = kind, what, skip_at
        self.echo = echo.strip() if echo and echo.strip() else None
        self.echo_seen: Optional[int] = None     # offset de la línea lógica del eco
        self.head: Optional[Line] = None
        self.text = ""
        self.done = False          # la línea lógica actual ya matcheó, es el eco o ya se evaluó

    def seed(self, f, start: int) -> None:
        segs = []
        for i, line in enumerate(_backward(f, start)):
            if i >= self.SEED_LINES:
                return
            if line.origin == "↪":
                segs.append(line.body)
            elif line.serial:
                self.head, self.text = line, line.body + "".join(reversed(segs))
                break
        else:
            return
        # Lo de antes de start ya lo vio el poll anterior: si ya era el eco o ya
        # matcheaba, no vuelve a contar; si no, sigue abierta para los `↪` del rango.
        text = clean_text(self.text)
        if self.echo and text.rstrip().endswith(self.echo):
            self.echo, self.echo_seen, self.done = None, self.head.offset, True
        else:
            self.done = self._test(text)

    def feed(self, line: Line):
        if line.origin == "↪":
            if self.head is None:
                return None
            self.text += line.body
        elif line.serial:
            self.head, self.text, self.done = line, line.body, False
        else:                      # taglog: línea propia, no corta la serial en curso
            return (line, line.clean()) if self.kind == "pattern" and self._test(line.clean()) else None
        if self.done or self.head.offset == self.skip_at:
            return None
        text = clean_text(self.text)
        if self.echo and text.rstrip().endswith(self.echo):
            self.echo, self.echo_seen, self.done = None, self.head.offset, True   # el eco no cuenta
            return None
        if self._test(text):
            self.done = True
            return (self.head, text)
        return None

    def _test(self, text: str) -> bool:
        if self.kind == "line":
            return line_kind(text) == self.what
        return bool(self.what.search(text))


# ---------- salida ----------

class _Output:
    """Filtro (src, grep), colapso del progreso de esptool y cabeza + cola."""

    def __init__(self, max_lines: int, grep, src: str, raw: bool):
        self.max = max_lines
        self.head_n = min(HEAD_LINES, max_lines // 2)
        self.grep, self.src, self.raw = grep, src, raw
        self.head: list = []
        self.tail = collections.deque(maxlen=max_lines - self.head_n)
        self.count = 0
        self.held: Optional[Line] = None
        self.date: Optional[str] = None

    def add(self, line: Line) -> None:
        if self.src == "serial" and not line.serial or self.src == "taglog" and line.origin != "|":
            return
        clean = line.clean()
        if self.grep is not None and not self.grep.search(clean):
            return
        if _PROGRESS_RE.search(clean):
            self.held = line                   # una racha de progreso queda en la última
            return
        self._flush_held()
        self._push(line)

    def _flush_held(self) -> None:
        if self.held is not None:
            line, self.held = self.held, None
            self._push(line)

    def _push(self, line: Line) -> None:
        if self.date is None and line.date is not None:
            self.date = line.date
        self.count += 1
        if len(self.head) < self.head_n:
            self.head.append(line)
        else:
            self.tail.append(line)

    def finish(self):
        self._flush_held()
        render = lambda ls: [l.render(self.date, self.raw) for l in ls]   # noqa: E731
        if self.count <= self.max:
            return render(self.head) + render(self.tail), False
        omitted = self.count - len(self.head) - len(self.tail)
        marker = "… 1 línea omitida …" if omitted == 1 else f"… {omitted} líneas omitidas …"
        return render(self.head) + [marker] + render(self.tail), True


def _server_time(now: float) -> str:
    return dt.datetime.fromtimestamp(now).astimezone().isoformat(timespec="milliseconds")


def _int(value, name: str, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise RangeError("bad_request", f"{name} tiene que ser un entero")
    if n < 0:
        raise RangeError("bad_request", f"{name} no puede ser negativo")
    return n


# ---------- API ----------

def read_range(home, since: Optional[str] = None, until: Optional[str] = None,
               around: Optional[str] = None, before=None, after=None,
               max_lines=None, grep: Optional[str] = None, src: Optional[str] = None,
               raw: bool = False, echo: Optional[str] = None,
               live: bool = True, now: Optional[float] = None) -> dict:
    """Rango del log (respuesta de §7.3). `live`: el proceso de la placa sigue
    escribiendo esa sesión (si no, session_ended). `echo`: texto mandado con
    send; la primera línea lógica que termina con él no cuenta para el match."""
    now = dt.datetime.now().timestamp() if now is None else now
    max_lines = max(1, min(_int(max_lines, "max_lines", DEFAULT_MAX_LINES), MAX_MAX_LINES))
    src = src or "all"
    if src not in ("all", "serial", "taglog"):
        raise RangeError("bad_request", "src tiene que ser serial, taglog o all")
    grep_re = Pattern(grep, "grep") if grep else None
    before, after = _int(before, "before"), _int(after, "after")
    if around and (since or until):
        raise RangeError("bad_request", "around no se combina con since/until")
    if (before is not None or after is not None) and not around:
        raise RangeError("bad_request", "before/after van con around")

    with BoardLog(home) as board:
        current = board.current_session()
        evs = _Events(board, current)
        until_found = match = matcher = match_at = None
        if around:
            start, limit = _around(board, around, before, after, now, evs)
        else:
            start = resolve(board, since or "session", now, evs)
            limit = start.sess.size
            if until:
                until_found = False
                kind, what = parse_until(until)
                skip_at = start.offset if start.etype == until else None
                if kind == "event":
                    # events.jsonl se relee después de fijar el tamaño del log (ver docstring)
                    hit = _find_event(evs, start, what, skip_at)
                    if hit is not None:
                        until_found, limit, match_line = True, hit[0], hit[1]
                        if match_line is not None:
                            match, match_at = match_line.render(None, raw), match_line.offset
                else:
                    matcher = _LogicalMatcher(kind, what, skip_at, echo)
                    matcher.seed(start.sess.f, start.offset)
        sess = start.sess
        out = _Output(max_lines, grep_re, src, raw)
        end = limit
        for line in _forward(sess.f, start.offset, limit):
            out.add(line)
            hit = matcher.feed(line) if matcher is not None else None
            if hit is not None:
                until_found, end, match_at = True, line.end, hit[0].offset
                match = _render_logical(hit[0], hit[1], raw, line)
                break
        lines, truncated = out.finish()
        if match is not None and out.date is not None:
            match = match if not match.startswith(out.date + " ") else match[len(out.date) + 1:]
        mine = evs.session(sess.sid, start.offset)
        evs_in = [compact_event(e) for e in mine if _event_key(e)[1] < end]
        if until_found and until in events.TYPES and not any(e["cursor"] == sess.cursor(end) for e in evs_in):
            evs_in += [compact_event(e) for e in mine
                       if e.get("type") == until and e.get("cursor") == sess.cursor(end)]
        echo_seen = None
        if matcher is not None and matcher.echo_seen is not None:
            echo_seen = sess.cursor(matcher.echo_seen)
        return {
            "date": out.date,
            "lines": lines,
            "start": sess.cursor(start.offset),
            "end": sess.cursor(end),
            "until_found": until_found,
            "match": match,
            "match_cursor": sess.cursor(match_at) if match_at is not None else None,
            "echo_seen": echo_seen,
            "partial": None,
            "truncated": truncated,
            "session_ended": sess.sid != current or not live,
            "events": evs_in,
            "server_time": _server_time(now),
        }


def _render_logical(head: Line, text: str, raw: bool, last: Line) -> str:
    """El match como línea lógica: hora y origen del `>` y el texto unido (con
    raw, los segmentos crudos)."""
    if head is last or head.origin == "|":
        return head.render(None, raw)
    if head.date is None:
        return text
    return f"{head.date} {head.time} {head.origin} {text}"


def _find_event(evs: _Events, start: Point, etype: str, skip_at: Optional[int]):
    """Primer evento `etype` desde start → (fin del rango, línea o None)."""
    sess = start.sess
    for ev in evs.session(sess.sid, start.offset):
        sid, off = _event_key(ev)
        if ev.get("type") != etype or off == skip_at:
            continue
        if off > sess.size:
            return None                   # después del tamaño fijado: lo ve el próximo poll
        off = _align_start(sess.f, off)
        for line in _forward(sess.f, off, sess.size):
            return line.end, line
        return off, None                  # apunta a la próxima línea, que todavía no está
    return None


def _around(board: BoardLog, anchor: str, before, after, now: float, evs: _Events):
    """--around E: del boot anterior a E (inclusive) al siguiente (exclusive), o
    before/after líneas alrededor de E (E incluida)."""
    center = resolve(board, anchor, now, evs)
    sess = center.sess
    if before is not None or after is not None:
        start = center.offset
        for i, line in enumerate(_backward(sess.f, center.offset)):
            if i >= (before or 0):
                break
            start = line.offset
        stop = center.offset
        for i, line in enumerate(_forward(sess.f, center.offset, sess.size)):
            if i > (after or 0):
                break
            stop = line.end
        return Point(sess, start), stop
    start = 0
    for line in _backward(sess.f, min(sess.size, _line_end(sess, center.offset))):
        if line.serial and line_kind(line.clean()) == "boot":
            start = line.offset
            break
    stop = sess.size
    for line in _forward(sess.f, center.offset, sess.size):
        if line.offset > center.offset and line.serial and line_kind(line.clean()) == "boot":
            stop = line.offset
            break
    return Point(sess, start), stop


def _line_end(sess: _Session, offset: int) -> int:
    for line in _forward(sess.f, offset, sess.size):
        return line.end
    return offset


def list_events(home, types: Optional[str] = None, since: Optional[str] = None,
                limit=None, order: Optional[str] = None, now: Optional[float] = None,
                counts: bool = False) -> dict:
    """Eventos de la placa ordenados por (sesión, offset). `since`: un anchor
    (inclusive); si es de tiempo, compara la hora del evento (cruza sesiones).
    `limit` (default 50): los ÚLTIMOS N; con order=asc, los PRIMEROS N desde
    since (para paginar hacia adelante). La lista siempre va en orden
    cronológico; `more`: quedaron eventos afuera (antes, o después con asc).
    `counts`: además {tipo: n} de todos los eventos desde since (sin el filtro
    de tipo ni el limit): el dashboard muestra los chips sin "cargar más"."""
    order = order or "desc"
    if order not in ("asc", "desc"):
        raise RangeError("bad_request", "order tiene que ser asc o desc")
    now = dt.datetime.now().timestamp() if now is None else now
    limit = max(1, min(_int(limit, "limit", 50), 1000))
    wanted = [t.strip() for t in (types or "").split(",") if t.strip()]
    unknown = [t for t in wanted if t not in events.TYPES]
    if unknown:
        raise RangeError("bad_request", f"tipos desconocidos: {', '.join(unknown)}")
    with BoardLog(home) as board:
        current = board.current_session()
        if not since and order == "desc":
            # Lo más común (el dashboard, el CLI): los últimos N, leyendo la cola
            page, more = _last_events(board.events_path, wanted, limit)
            resp = {"events": [compact_event(e) for e in page], "more": more,
                    "session": current, "server_time": _server_time(now)}
            if counts:
                resp["counts"] = all_counts(board.events_path)
            return resp
        out = None
        if since:
            t = parse_time(since, now) if not since.startswith("c:") else None
            if t is None:
                evs = _Events(board, current)
                p = resolve(board, since, now, evs)
                if p.sess.sid == current and not counts:     # la actual: sus eventos son la cola del archivo
                    out = sort_events(_session_tail(board.events_path, current, p.offset, wanted))
                elif p.sess.sid == current:
                    out = evs.session(current, p.offset)
                else:
                    out = [e for e in sort_events(board.read_events()) if _event_key(e) >= (p.sess.sid, p.offset)]
            else:
                out = [e for e in sort_events(board.read_events())
                       if _event_epoch(e) is not None and _event_epoch(e) >= t]
        else:
            out = sort_events(board.read_events())
        by_type = {}
        if counts:
            for e in out:
                by_type[e.get("type")] = by_type.get(e.get("type"), 0) + 1
        if wanted:
            out = [e for e in out if e.get("type") in wanted]
        page = out[:limit] if order == "asc" else out[-limit:]
        resp = {"events": [compact_event(e) for e in page], "more": len(out) > limit,
                "session": current, "server_time": _server_time(now)}
        if counts:
            resp["counts"] = by_type
        return resp


def _event_epoch(ev: dict) -> Optional[float]:
    try:
        return dt.datetime.fromisoformat(ev.get("ts")).timestamp()
    except (TypeError, ValueError):
        return None

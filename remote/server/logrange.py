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
"""
import collections
import datetime as dt
import os
import pathlib
import re
from typing import Optional

from server import events
from server.serial_watch import line_kind

DEFAULT_MAX_LINES = 200
MAX_MAX_LINES = 5000
HEAD_LINES = 50
SLACK = 2.0              # s; > MAX_HOLD de DeviceLog: las horas del archivo no son monótonas
LINE_TYPES = ("boot", "panic")   # until que se buscan en las líneas

_PREFIX_RE = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d\.\d{3}) ([>|↪]) ")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROGRESS_RE = re.compile(r"\b(?:Writing|Reading) at 0x[0-9a-fA-F]+")
_EVENT_RE = re.compile(r"([a-z_]+)(?:~(\d+))?")
_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)(s|m|h|d)")
_CLOCK_RE = re.compile(r"(\d{1,2}):(\d\d)(?::(\d\d)(?:\.(\d{1,6}))?)?")
_ISO_RE = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d{1,6})?)?(?:Z|[+-]\d\d:?\d\d)?")
_DUR_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class RangeError(Exception):
    """error: bad_anchor | cursor_expired | not_found | bad_request (el contrato
    del CLI, §8.3)."""

    def __init__(self, error: str, message: str):
        super().__init__(message)
        self.error = error
        self.message = message


# ---------- líneas ----------

class Line:
    __slots__ = ("offset", "end", "date", "time", "origin", "body")

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

    @property
    def serial(self) -> bool:
        return self.origin in (">", "↪", None)

    def clean(self) -> str:
        return clean_text(self.body)

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


def resolve(board: BoardLog, anchor: str, now: float, evs: Optional[list] = None) -> Point:
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
        mine = [e for e in sort_events(evs if evs is not None else board.read_events())
                if e.get("type") == etype and _event_key(e)[0] == sess.sid and _event_key(e)[1] <= sess.size]
        if n >= len(mine):
            raise RangeError("bad_anchor", f"no hay {anchor} en la sesión actual ({len(mine)} {etype})")
        return Point(sess, _align_start(sess.f, _event_key(mine[-1 - n])[1]), etype)
    t = parse_time(anchor, now)
    if t is not None:
        return Point(sess, _first_at_or_after(sess, t))
    raise RangeError("bad_anchor", f"anchor desconocido: {anchor}")


# ---------- until ----------

def parse_until(until: str):
    """→ ("event", tipo) | ("line", tipo) | ("pattern", regex compilada)."""
    if until in LINE_TYPES:
        return ("line", until)
    if until in events.TYPES:
        return ("event", until)
    if until.startswith("re:"):
        try:
            return ("pattern", re.compile(until[3:]))
        except re.error as e:
            raise RangeError("bad_request", f"regex inválida en until: {e}")
    return ("pattern", re.compile(re.escape(until)))


class _LogicalMatcher:
    """Evalúa patrones / tipos de línea sobre líneas lógicas: un `>` más sus
    `↪` (con taglog intercalado). Devuelve la línea donde se completó el match
    y la línea `>` donde empieza."""

    def __init__(self, kind: str, what, skip_at: Optional[int], echo: Optional[str]):
        self.kind, self.what, self.skip_at = kind, what, skip_at
        self.echo = echo.strip() if echo else None
        self.head: Optional[Line] = None
        self.text = ""
        self.done = False          # la línea lógica actual ya matcheó o es el eco

    def feed(self, line: Line) -> Optional[Line]:
        if line.origin == "↪":
            if self.head is None:
                return None
            self.text += line.body
        elif line.serial:
            self.head, self.text, self.done = line, line.body, False
        else:                      # taglog: línea propia, no corta la serial en curso
            return line if self.kind == "pattern" and self._test(line.clean()) else None
        if self.done or self.head.offset == self.skip_at:
            return None
        text = clean_text(self.text)
        if self.echo and text.rstrip().endswith(self.echo):
            self.echo, self.done = None, True      # el eco del comando no cuenta
            return None
        if self._test(text):
            self.done = True
            return self.head
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
    try:
        grep_re = re.compile(grep) if grep else None
    except re.error as e:
        raise RangeError("bad_request", f"regex inválida en grep: {e}")
    before, after = _int(before, "before"), _int(after, "after")
    if around and (since or until):
        raise RangeError("bad_request", "around no se combina con since/until")
    if (before is not None or after is not None) and not around:
        raise RangeError("bad_request", "before/after van con around")

    with BoardLog(home) as board:
        current = board.current_session()
        evs = board.read_events()
        if around:
            start, stop = _around(board, around, before, after, now, evs)
            until_found, match_line, end = None, None, stop
        else:
            start = resolve(board, since or "session", now, evs)
            until_found, match_line, end = None, None, start.sess.size
            if until:
                # events.jsonl se relee después de fijar el tamaño del log (ver docstring)
                until_found, match_line, end = _find_until(board, start, until, echo)
        sess = start.sess
        out = _Output(max_lines, grep_re, src, raw)
        for line in _forward(sess.f, start.offset, end):
            out.add(line)
        lines, truncated = out.finish()
        evs_in = [compact_event(e) for e in sort_events(board.read_events())
                  if _event_key(e)[0] == sess.sid and start.offset <= _event_key(e)[1] < end]
        if until_found and match_line is None and until in events.TYPES:
            evs_in += [compact_event(e) for e in board.read_events()
                       if e.get("type") == until and e.get("cursor") == sess.cursor(end)]
        match = None
        if match_line is not None:
            match = match_line.render(match_line.date if out.date is None else out.date, raw)
        return {
            "date": out.date,
            "lines": lines,
            "start": sess.cursor(start.offset),
            "end": sess.cursor(end),
            "until_found": until_found,
            "match": match,
            "partial": None,
            "truncated": truncated,
            "session_ended": sess.sid != current or not live,
            "events": evs_in,
            "server_time": _server_time(now),
        }


def _find_until(board: BoardLog, start: Point, until: str, echo: Optional[str]):
    """→ (until_found, línea del match o None, fin del rango)."""
    sess = start.sess
    kind, what = parse_until(until)
    skip_at = start.offset if start.etype == until else None
    if kind == "event":
        for ev in sort_events(board.read_events()):
            sid, off = _event_key(ev)
            if ev.get("type") != what or sid != sess.sid or off < start.offset or off == skip_at:
                continue
            if off > sess.size:
                break                     # después del tamaño fijado: lo ve el próximo poll
            off = _align_start(sess.f, off)
            for line in _forward(sess.f, off, sess.size):
                return True, line, line.end
            return True, None, off        # apunta a la próxima línea, que todavía no está
        return False, None, sess.size
    matcher = _LogicalMatcher(kind, what, skip_at, echo)
    for line in _forward(sess.f, start.offset, sess.size):
        head = matcher.feed(line)
        if head is not None:
            return True, line if head is line else head, line.end
    return False, None, sess.size


def _around(board: BoardLog, anchor: str, before, after, now: float, evs: list):
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
                limit=None, now: Optional[float] = None) -> dict:
    """Eventos de la placa ordenados por (sesión, offset). `since`: un anchor;
    si es de tiempo, compara la hora del evento (cruza sesiones)."""
    now = dt.datetime.now().timestamp() if now is None else now
    limit = max(1, min(_int(limit, "limit", 50), 1000))
    wanted = [t.strip() for t in (types or "").split(",") if t.strip()]
    unknown = [t for t in wanted if t not in events.TYPES]
    if unknown:
        raise RangeError("bad_request", f"tipos desconocidos: {', '.join(unknown)}")
    with BoardLog(home) as board:
        evs = board.read_events()
        current = board.current_session()
        out = sort_events(evs)
        if since:
            t = parse_time(since, now) if not since.startswith("c:") else None
            if t is not None:
                out = [e for e in out if _event_epoch(e) is not None and _event_epoch(e) >= t]
            else:
                p = resolve(board, since, now, evs)
                out = [e for e in out if _event_key(e) >= (p.sess.sid, p.offset)]
        if wanted:
            out = [e for e in out if e.get("type") in wanted]
        return {"events": [compact_event(e) for e in out[-limit:]], "session": current,
                "server_time": _server_time(now)}


def _event_epoch(ev: dict) -> Optional[float]:
    try:
        return dt.datetime.fromisoformat(ev.get("ts")).timestamp()
    except (TypeError, ValueError):
        return None

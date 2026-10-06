#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
device_log.py — DeviceLog: único escritor del log de un dispositivo.

Reemplaza a `tmux pipe-pane` (que capturaba a ciegas todo lo que salía por la
terminal) y al serial.log que EspMonitor escribía sin que nadie lo leyera.
Recibe el serial crudo (write_serial, desde EspMonitor) y las líneas de
taglog (write_taglog) — o sea serial + flash + resets, todo en un archivo,
que es lo que muestra el dashboard.

Una sola tubería de líneas (docs/specs/agents-cli.md §3):

    serial ─bytes→ write_serial: decoder UTF-8 incremental, corte en \\n
    taglog ───────→ write_taglog
                      └→ "<YYYY-MM-DD HH:MM:SS.mmm> <origen> <cuerpo>\\n"

- Origen: `>` serial, `|` taglog, `↪` continuación de una línea serial.
- La línea serial en curso se retiene en memoria hasta el \\n, hasta
  PARTIAL_TIMEOUT sin bytes nuevos (un prompt de esp_console no termina en \\n)
  o hasta MAX_HOLD desde su primer byte aunque sigan llegando (una línea que
  gotea, "Connecting.....", no queda retenida para siempre).
  Si después llega más de la misma línea, sale con origen `↪`. Así una línea
  de taglog de otro hilo nunca queda pegada a una serial.
- El archivo se escribe en binario y el offset se lleva sumando bytes: cada
  línea completa termina en un offset conocido. Cursor = c:<sesión>:<offset>.
- line_sink(texto, cursor, ts) recibe cada línea serial completa (la lógica:
  segmento + continuaciones), con el cursor del inicio de la línea. Es el
  SerialWatch.
- event(...) escribe en events.jsonl (events.py) con el cursor exacto. Antes
  de la MAC los eventos se retienen con su posición en el buffer y se
  recalculan al volcarlo.

Ciclo de vida:
- Sesión = una ejecución del proceso: session_id = YYYYMMDD_HHMMSS_<pid>.
- Antes de conocer la MAC, bufferea líneas en memoria (con tope).
- adopt(mac): abre devices/<mac>/output.log, escribe el header de sesión (y
  el evento `session`) y vuelca el buffer. El header va fuera del buffer (el buffer descarta desde
  el principio cuando se llena).
- adopt_unknown(): si la MAC no se pudo leer, abre devices/unknown-<tty>/
  output.log para no bufferear para siempre. Si la MAC aparece después
  (fallback por serial), adopt(mac) migra el contenido al archivo de la MAC
  (al principio, así los offsets no cambian) y borra el provisorio. De
  events.jsonl migran solo los eventos de esta sesión.

Un archivo por sesión: al abrir, si ya hay un output.log con contenido de
una sesión anterior, se rota a output_<session_id>.log (el id sale de su
header; un log viejo sin header usa la hora actual).
"""
import codecs
import contextlib
import datetime as dt
import os
import pathlib
import re
import threading
import time
from typing import Callable, Optional

from server import events, paths, taglog
from server.events import format_cursor, parse_cursor, read_session_id  # noqa: F401 (API del módulo)

TAG = "devicelog"

BUFFER_LIMIT = 256 * 1024   # bytes de líneas retenidas antes de saber dónde escribir
PARTIAL_TIMEOUT = 0.150     # segundos sin bytes nuevos para escribir una línea serial sin \n
MAX_HOLD = 1.0              # segundos como mucho que se retiene un segmento (acota el desorden de horas)
MAX_LINE = 4096             # caracteres: una línea serial más larga se corta (sigue con ↪)

ORIGIN_SERIAL = ">"
ORIGIN_TAGLOG = "|"
ORIGIN_CONT = "↪"

PREFIX_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} [>|↪] ")


def format_ts(epoch: float) -> str:
    """YYYY-MM-DD HH:MM:SS.mmm en hora local."""
    d = dt.datetime.fromtimestamp(epoch)
    return d.strftime("%Y-%m-%d %H:%M:%S.") + f"{d.microsecond // 1000:03d}"


def make_session_id(epoch: float, pid: int) -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(epoch)) + f"_{pid}"


class _BufPos:
    """Posición de una línea mientras el log está en el buffer pre-MAC: bytes
    desde el inicio del buffer (contando lo descartado). Se traduce a offset
    de archivo al volcar el buffer."""
    __slots__ = ("n",)

    def __init__(self, n: int):
        self.n = n


class DeviceLog:
    """Log de un dispositivo. Una instancia por proceso (por tty)."""

    def __init__(self, tty_name: str, buffer_limit: int = BUFFER_LIMIT,
                 tcp_port: Optional[int] = None,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 partial_timeout: float = PARTIAL_TIMEOUT,
                 max_hold: float = MAX_HOLD,
                 autoflush: bool = True):
        """autoflush: un hilo escribe la línea parcial a los partial_timeout.
        Con False hay que llamar a tick() (tests con reloj falso)."""
        self.tty_name = tty_name
        self.tcp_port = tcp_port
        self._clock = clock
        self._mono = monotonic
        self._partial_timeout = partial_timeout
        self._max_hold = max_hold
        self._autoflush = autoflush
        self.started_at = clock()
        self.session_id = make_session_id(self.started_at, os.getpid())
        self.line_sink: Optional[Callable[[str, object, float], None]] = None

        # RLock: un taglog emitido con el lock tomado (error al escribir un
        # evento) vuelve a entrar por taglog_sink en el mismo hilo.
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._flusher: Optional[threading.Thread] = None
        self._closed = False
        self._mac: Optional[str] = None
        self._fh = None
        self._path: Optional[pathlib.Path] = None
        self._offset = 0                     # bytes escritos en el archivo actual
        # buffer pre-MAC: líneas ya codificadas
        self._buffer: list = []
        self._buffered = 0
        self._buffer_limit = buffer_limit
        self._buf_stream = 0                 # bytes que entraron al buffer (incluye descartados)
        self._dropped = 0                    # bytes descartados por tope
        self._buf_base: Optional[tuple] = None   # (offset del buffer en el archivo, descartados)
        self._pending_events: list = []      # eventos pre-MAC: (type, detail, _BufPos, ts, by)
        # serial
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pend: Optional[str] = None     # segmento serial retenido (sin \n todavía)
        self._pend_ts = 0.0
        self._pend_last = 0.0                # monotonic del último byte
        self._pend_first = 0.0               # monotonic del primer byte del segmento
        self._cont = False                   # la línea lógica ya tiene un segmento escrito
        self._line_text = ""                 # línea lógica (para line_sink), con tope
        self._line_pos = None
        self._line_ts = 0.0

    @property
    def adopted(self) -> bool:
        """Ya tiene archivo destino (MAC o provisorio), aunque esté cerrado."""
        return self._path is not None

    @property
    def path(self) -> Optional[pathlib.Path]:
        """Archivo actual, o None si todavía está buffereando."""
        return self._path

    @property
    def events_path(self) -> Optional[pathlib.Path]:
        return paths.events_file_beside(self._path) if self._path is not None else None

    def end_cursor(self) -> Optional[str]:
        """Cursor del fin de la última línea completa escrita (None antes de adoptar)."""
        with self._lock:
            if self._path is None:
                return None
            return format_cursor(self.session_id, self._offset)

    # ---------- escritura ----------

    def write_serial(self, data: bytes) -> None:
        """Serial crudo del PTY, en pedazos arbitrarios. El decoder incremental
        evita romper un carácter UTF-8 partido entre dos lecturas."""
        now, mono = self._clock(), self._mono()
        text = self._decoder.decode(data)
        done = []
        with self._lock:
            pieces = text.split("\n")
            for i, piece in enumerate(pieces):
                if piece:
                    if self._pend is None:
                        self._pend, self._pend_ts, self._pend_first = piece, now, mono
                        if not self._cont:
                            self._line_text, self._line_ts = "", now
                    else:
                        self._pend += piece
                    if len(self._line_text) < MAX_LINE:
                        self._line_text = (self._line_text + piece)[:MAX_LINE]
                    while len(self._pend) > MAX_LINE:   # basura sin fin de línea: cortar
                        rest = self._pend[MAX_LINE:]
                        self._pend = self._pend[:MAX_LINE]
                        self._write_pending()
                        self._pend, self._pend_ts, self._pend_first = rest, now, mono
                if i < len(pieces) - 1:
                    done.append(self._end_line(now))
            if data:
                self._pend_last = mono
            if self._pend is not None:
                self._start_flusher()
        sink = self.line_sink
        if sink is not None:
            for text_, pos, ts in done:
                try:
                    sink(text_, pos, ts)
                except Exception as e:
                    taglog.debug(TAG, f"line_sink: {e}")

    def write_taglog(self, level: str, tag: str, msg: str) -> None:
        now = self._clock()
        with self._lock:
            for line in (msg.splitlines() or [""]):
                self._emit(ORIGIN_TAGLOG, now, taglog.format_body(level, tag, line))

    def taglog_sink(self, ts: str, level: str, tag: str, msg: str) -> None:
        """Sink de taglog. La hora del prefijo es la propia (con milisegundos).
        DEBUG queda solo en la terminal tmux (devremote <dev>): el log del device
        es lo que se lee en el dashboard."""
        if level == "DEBUG":
            return
        self.write_taglog(level, tag, msg)

    def tick(self) -> None:
        """Escribe la línea serial retenida si pasó partial_timeout sin bytes
        nuevos o max_hold desde su primer byte."""
        with self._lock:
            if self._pend is not None and self._flush_delay() <= 0:
                self._write_pending()

    @contextlib.contextmanager
    def atomic(self):
        """Lo que se haga adentro (event() + la línea taglog que lo acompaña) no
        se intercala con líneas de otros hilos: el cursor del evento cae en la
        línea. El lock es reentrante: taglog vuelve a entrar por taglog_sink."""
        with self._lock:
            yield

    def event(self, type_: str, detail: Optional[dict] = None, cursor=None,
              ts: Optional[float] = None, by: str = "device") -> Optional[str]:
        """Registra un evento en events.jsonl. cursor: el que recibió line_sink
        (str o posición del buffer), o None = fin de la última línea escrita.
        Devuelve el cursor del evento (None si quedó pendiente pre-MAC o si el
        log está cerrado)."""
        ts = self._clock() if ts is None else ts
        with self._lock:
            if self._closed:            # desconectado: no hay línea a la que apuntar
                return None
            pos = self._pos() if cursor is None else cursor
            if isinstance(pos, _BufPos):
                if self._buf_base is None:          # todavía en el buffer pre-MAC
                    self._pending_events.append((type_, detail, pos, ts, by))
                    return None
                pos = self._buf_to_offset(pos)
            if isinstance(pos, int):
                pos = format_cursor(self.session_id, pos)
            self._append_event(events.make(type_, pos, detail, by=by, ts=ts))
            return pos

    # ---------- adopción ----------

    def adopt(self, mac: str) -> None:
        """Pasa a escribir en devices/<mac>/output.log. No-op si ya tiene MAC."""
        with self._lock:
            if self._mac is not None:
                return
            self._mac = mac
            target = paths.device_output_log(mac)
            if self._fh is not None:  # venía escribiendo en el provisorio
                self._migrate(target)
                self._emit(ORIGIN_TAGLOG, self._clock(),
                           taglog.format_body("INFO", TAG, f"--- MAC resuelta: {mac} ---"))
                return
            self._open(target)
            self._flush_buffer()

    def adopt_unknown(self) -> None:
        """La MAC no se pudo leer: escribir en el hogar provisorio por tty."""
        with self._lock:
            if self._path is not None:
                return
            self._open(paths.unknown_output_log(self.tty_name))
            self._flush_buffer()

    def close(self) -> None:
        with self._lock:
            if self._pend is not None:
                self._write_pending()
            self._closed = True
            self._cond.notify_all()
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    # ---------- internos (con lock tomado) ----------

    def _pos(self):
        """Dónde va a caer la próxima línea: offset de archivo o _BufPos."""
        if self._path is not None:
            return self._offset
        return _BufPos(self._buf_stream)

    def _emit(self, origin: str, ts: float, body: str) -> None:
        data = f"{format_ts(ts)} {origin} {body}\n".encode("utf-8", errors="replace")
        if self._fh is not None:
            self._write_all(data)
            return
        if self._path is not None:     # cerrado (desconexión): no hay dónde escribir
            return
        self._buffer.append(data)
        self._buffered += len(data)
        self._buf_stream += len(data)
        while self._buffered > self._buffer_limit and len(self._buffer) > 1:
            dropped = self._buffer.pop(0)
            self._buffered -= len(dropped)
            self._dropped += len(dropped)

    def _write_all(self, data: bytes) -> None:
        """Archivo sin buffer: un write() por línea, que puede escribir menos de lo
        pedido. El offset cuenta solo lo que llegó al archivo."""
        view = memoryview(data)
        while view:
            n = self._fh.write(view)
            if not n:
                raise OSError(f"escritura corta en {self._path}")
            self._offset += n
            view = view[n:]

    def _write_pending(self) -> None:
        """Escribe el segmento serial retenido; lo que siga de esa línea sale con ↪.
        Una continuación vacía (el \\r de un \\r\\n que llegó tarde) no se escribe."""
        body = self._pend.rstrip("\r")
        self._pend = None
        if self._cont and not body:
            return
        if not self._cont:
            self._line_pos = self._pos()
        self._emit(ORIGIN_CONT if self._cont else ORIGIN_SERIAL, self._pend_ts, body)
        self._cont = True

    def _flush_delay(self) -> float:
        """Segundos hasta que hay que escribir el segmento retenido (<= 0: ya)."""
        now = self._mono()
        return min(self._pend_last + self._partial_timeout, self._pend_first + self._max_hold) - now

    def _end_line(self, now: float):
        """Llegó un \\n: cierra la línea lógica. Devuelve (texto, cursor, ts) para line_sink."""
        if self._pend is not None:
            self._write_pending()
        elif not self._cont:                      # línea vacía
            self._line_ts = now
            self._line_pos = self._pos()
            self._emit(ORIGIN_SERIAL, now, "")
        item = (self._line_text.rstrip("\r"), self._cursor_of(self._line_pos), self._line_ts)
        self._cont = False
        self._line_text = ""
        return item

    def _cursor_of(self, pos):
        if isinstance(pos, _BufPos):
            return pos
        return format_cursor(self.session_id, pos)

    def _start_flusher(self) -> None:
        if not self._autoflush or self._closed:
            self._cond.notify_all()
            return
        if self._flusher is None:
            self._flusher = threading.Thread(target=self._flush_loop, name=f"devicelog-{self.tty_name}",
                                             daemon=True)
            self._flusher.start()
        else:
            self._cond.notify_all()

    def _flush_loop(self) -> None:
        with self._cond:
            while not self._closed:
                if self._pend is None:
                    self._cond.wait()
                    continue
                delay = self._flush_delay()
                if delay > 0:
                    self._cond.wait(delay)
                    continue
                try:
                    self._write_pending()
                except Exception as e:      # disco lleno, archivo cerrado...: el hilo no muere
                    self._pend = None
                    taglog.debug(TAG, f"flush de línea parcial: {e}")

    def _append_event(self, ev: dict) -> None:
        try:
            events.append(self.events_path, ev)
        except OSError as e:
            taglog.warn(TAG, f"no se pudo registrar el evento {ev['type']}: {e}")

    def _open(self, target: pathlib.Path, header: bool = True) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if header:
            _rotate(target)
        self._fh = open(target, "ab", buffering=0)
        self._path = target
        self._offset = self._fh.seek(0, os.SEEK_END)
        if header:
            self._emit(ORIGIN_TAGLOG, self.started_at, taglog.format_body(
                "INFO", TAG, f"sesión {self.session_id} tty={self.tty_name}"))
            self._append_event(events.make(
                "session", format_cursor(self.session_id, 0),
                {"tty": self.tty_name, "tcp_port": self.tcp_port, "pid": os.getpid()}, ts=self.started_at))

    def _flush_buffer(self) -> None:
        """Vuelca el buffer pre-MAC después del header. Los marcadores van después
        del buffer: así los offsets relativos al buffer siguen valiendo."""
        self._buf_base = (self._offset, self._dropped)
        for chunk in self._buffer:
            self._write_all(chunk)
        for type_, detail, pos, ts, by in self._pending_events:
            self._append_event(events.make(type_, format_cursor(self.session_id, self._buf_to_offset(pos)),
                                           detail, by=by, ts=ts))
        self._pending_events.clear()
        if not self._buffer and not self._dropped:
            return
        now = self._clock()
        self._emit(ORIGIN_TAGLOG, now, taglog.format_body(
            "INFO", TAG, f"--- adoptado desde tty={self.tty_name} @ {format_ts(now)} ---"))
        if self._dropped:
            self._emit(ORIGIN_TAGLOG, now, taglog.format_body(
                "WARN", TAG, f"--- {self._dropped} bytes descartados al principio (buffer lleno) ---"))
        self._buffer.clear()
        self._buffered = 0

    def _buf_to_offset(self, pos: _BufPos) -> int:
        """_BufPos → offset en el archivo, una vez volcado el buffer. Lo que se
        descartó cae al inicio del buffer."""
        base, dropped = self._buf_base
        return base + max(0, pos.n - dropped)

    def _migrate(self, target: pathlib.Path) -> None:
        """Provisorio → archivo de la MAC. El contenido va al principio (después
        de rotar la sesión anterior de esa MAC): los offsets no cambian. Los
        eventos de esta sesión pasan al events.jsonl de la MAC."""
        prev = self._path
        prev_events = self.events_path
        self._fh.close()
        try:
            content = prev.read_bytes()
        except OSError:
            content = b""
        _rotate(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as f:
            f.write(content)
        self._open(target, header=False)
        try:
            events.migrate_session(prev_events, self.events_path, self.session_id)
        except OSError as e:
            taglog.warn(TAG, f"no se pudieron migrar los eventos de {prev_events}: {e}")
        try:
            prev.unlink()
            prev.parent.rmdir()
        except OSError:
            pass


def _rotate(target: pathlib.Path) -> None:
    """output.log con contenido → output_<session_id>.log (del header). Un log
    sin header (anterior a este formato) usa la hora actual."""
    try:
        if target.stat().st_size == 0:
            return
    except FileNotFoundError:
        return
    stamp = read_session_id(target) or time.strftime("%Y%m%d_%H%M%S")
    dest = target.with_name(f"output_{stamp}.log")
    n = 1
    while dest.exists():
        dest = target.with_name(f"output_{stamp}_{n}.log")
        n += 1
    target.rename(dest)

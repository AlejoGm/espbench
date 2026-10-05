#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
device_log.py — DeviceLog: único escritor del log de un dispositivo.

Reemplaza a `tmux pipe-pane` (que capturaba a ciegas todo lo que salía por la
terminal) y al serial.log que EspMonitor escribía sin que nadie lo leyera.
Recibe el serial crudo (write_bytes, desde EspMonitor) y las líneas de
taglog (taglog_sink) — o sea serial + flash + resets, todo en un archivo,
que es lo que muestra el dashboard.

Ciclo de vida:
- Antes de conocer la MAC, bufferea en memoria (con tope).
- adopt(mac): abre devices/<mac>/output.log y vuelca el buffer.
- adopt_unknown(): si la MAC no se pudo leer, abre devices/unknown-<tty>/
  output.log para no bufferear para siempre. Si la MAC aparece después
  (fallback por serial), adopt(mac) migra el contenido al archivo de la MAC
  y borra el provisorio.

Un archivo por sesión: al abrir, si ya hay un output.log con contenido de
una sesión anterior, se rota a output_<ts>.log (mismo comportamiento que
tenía esp32_tmux.sh con `mv`).
"""
import codecs
import pathlib
import threading
import time
from typing import Optional

from server import paths, taglog

BUFFER_LIMIT = 256 * 1024  # caracteres retenidos antes de saber dónde escribir


class DeviceLog:
    """Log de un dispositivo. Una instancia por proceso (por tty)."""

    def __init__(self, tty_name: str, buffer_limit: int = BUFFER_LIMIT):
        self.tty_name = tty_name
        self._lock = threading.Lock()
        self._mac: Optional[str] = None
        self._buffer: list = []
        self._buffered = 0
        self._buffer_limit = buffer_limit
        self._dropped = 0
        self._fh = None
        self._path: Optional[pathlib.Path] = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    @property
    def adopted(self) -> bool:
        """Ya tiene archivo destino (MAC o provisorio), aunque esté cerrado."""
        return self._path is not None

    @property
    def path(self) -> Optional[pathlib.Path]:
        """Archivo actual, o None si todavía está buffereando."""
        return self._path

    # ---------- escritura ----------

    def write(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            if self._fh is None:
                self._buffer.append(text)
                self._buffered += len(text)
                while self._buffered > self._buffer_limit and len(self._buffer) > 1:
                    dropped = self._buffer.pop(0)
                    self._buffered -= len(dropped)
                    self._dropped += len(dropped)
                return
            self._fh.write(text)
            self._fh.flush()

    def write_bytes(self, data: bytes) -> None:
        """Serial crudo del PTY. El decoder incremental evita romper un carácter
        UTF-8 que quedó partido entre dos lecturas."""
        self.write(self._decoder.decode(data))

    def taglog_sink(self, ts: str, level: str, tag: str, msg: str) -> None:
        self.write(taglog.format_line(ts, level, tag, msg) + "\n")

    # ---------- adopción ----------

    def adopt(self, mac: str) -> None:
        """Pasa a escribir en devices/<mac>/output.log. No-op si ya tiene MAC."""
        with self._lock:
            if self._mac is not None:
                return
            self._mac = mac
            target = paths.device_output_log(mac)
            if self._fh is not None:  # venía escribiendo en el provisorio
                self._migrate(target, f"--- MAC resuelta: {mac} ---\n")
                return
            self._open(target)
            self._flush_buffer()

    def adopt_unknown(self) -> None:
        """La MAC no se pudo leer: escribir en el hogar provisorio por tty."""
        with self._lock:
            if self._path is not None:
                return
            self._open(paths.device_unknown_home(self.tty_name) / "output.log")
            self._flush_buffer()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    # ---------- internos (con lock tomado) ----------

    def _open(self, target: pathlib.Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate(target)
        self._fh = target.open("a", encoding="utf-8", errors="replace")
        self._path = target

    def _flush_buffer(self) -> None:
        if self._buffer or self._dropped:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self._fh.write(f"--- adoptado desde tty={self.tty_name} @ {ts} ---\n")
            if self._dropped:
                self._fh.write(f"--- {self._dropped} caracteres descartados (buffer lleno) ---\n")
            for chunk in self._buffer:
                self._fh.write(chunk)
            self._buffer.clear()
            self._buffered = 0
            self._dropped = 0
        self._fh.flush()

    def _migrate(self, target: pathlib.Path, marker: str) -> None:
        prev = self._path
        self._fh.close()
        try:
            content = prev.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        self._open(target)
        self._fh.write(content)
        self._fh.write(marker)
        self._fh.flush()
        try:
            prev.unlink()
            prev.parent.rmdir()
        except OSError:
            pass


def _rotate(target: pathlib.Path) -> None:
    try:
        if target.stat().st_size == 0:
            return
    except FileNotFoundError:
        return
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = target.with_name(f"output_{stamp}.log")
    n = 1
    while dest.exists():
        dest = target.with_name(f"output_{stamp}_{n}.log")
        n += 1
    target.rename(dest)

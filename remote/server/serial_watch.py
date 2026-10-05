#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serial_watch.py — SerialWatch: lee el serial del device línea a línea y saca
de ahí la salud del firmware y su identificación.

Detecta:
- reinicios, con el motivo que imprime la ROM (rst:0xc (SW_CPU_RESET), ...)
- panics: Guru Meditation, abort(), brownout, task watchdog, stack overflow,
  assert
- boot loop: varios reinicios en poco tiempo
- info de firmware: proyecto, versión e IDF (lo que imprime app_init al bootear)

Corre en el proceso del device porque es el único que ve todo el serial en
tiempo real. Antes la info de firmware la parseaba el dashboard, solo mientras
alguien tenía abierta la página del device, así que la card mostraba la
versión vieja después de un flash. El resultado lo publica el Device en
run/<tty>.json (ver device.py), y de ahí lo lee el dashboard.
"""
import codecs
import collections
import datetime as dt
import re
import threading
import time
from typing import Callable, Optional

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_RESET_RE = re.compile(r"rst:0x[0-9a-fA-F]+ \(([A-Z0-9_]+)\)")
_FW_RES = {
    "project": re.compile(r"(?:app_init|cpu_start): Project name:\s+(\S+)"),
    "version": re.compile(r"(?:app_init|cpu_start): App version:\s+(\S+)"),
    "idf": re.compile(r"(?:app_init|cpu_start): ESP-IDF:\s+(\S+)"),
}
# (regex, tipo). El primer grupo, si hay, va como detalle.
_PANIC_RES = [
    (re.compile(r"Guru Meditation Error: Core\s+\d+ panic'ed \(([^)]*)\)"), "panic"),
    (re.compile(r"abort\(\) was called"), "abort"),
    (re.compile(r"Brownout detector was triggered"), "brownout"),
    (re.compile(r"Task watchdog got triggered"), "task_wdt"),
    (re.compile(r"\*\*\*ERROR\*\*\* A stack overflow in task (\S+)"), "stack_overflow"),
    (re.compile(r"assert failed:"), "assert"),
]
# Motivos de reinicio que por sí solos ya indican un problema.
_ABNORMAL_RESET = ("WDT", "BROWNOUT", "PANIC")

MAX_LINE = 4096
BOOT_LOOP_COUNT = 3
BOOT_LOOP_WINDOW = 120.0  # segundos


def _now_iso(clock: Callable[[], float]) -> str:
    return dt.datetime.fromtimestamp(clock()).isoformat(timespec="seconds")


class SerialWatch:

    def __init__(self, on_change: Optional[Callable[[], None]] = None,
                 clock: Callable[[], float] = time.time,
                 boot_loop_count: int = BOOT_LOOP_COUNT,
                 boot_loop_window: float = BOOT_LOOP_WINDOW):
        self._on_change = on_change
        self._clock = clock
        self._loop_count = boot_loop_count
        self._loop_window = boot_loop_window
        self._lock = threading.Lock()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._partial = ""
        self.boots = 0
        self.panics = 0
        self.last_reset: Optional[dict] = None
        self.last_panic: Optional[dict] = None
        self.fw: dict = {}
        self._boot_times = collections.deque()
        self.since = _now_iso(clock)

    def reset_counters(self) -> None:
        """Antes de un flash o un erase: los dos reinician el chip a propósito, y
        no tienen que contar como problema. La info de firmware se conserva."""
        with self._lock:
            self.boots = self.panics = 0
            self.last_reset = self.last_panic = None
            self._boot_times.clear()
            self.since = _now_iso(self._clock)

    # ---------- entrada ----------

    def feed(self, data: bytes) -> None:
        """Bytes crudos del PTY, en pedazos arbitrarios."""
        text = self._partial + self._decoder.decode(data)
        lines = text.replace("\r", "\n").split("\n")
        self._partial = lines.pop()
        if len(self._partial) > MAX_LINE:     # basura sin fin de línea: no acumular
            lines.append(self._partial)
            self._partial = ""
        changed = False
        for line in lines:
            if line:
                changed |= self._line(_ANSI_RE.sub("", line))
        if changed and self._on_change:
            self._on_change()

    # ---------- salida ----------

    @property
    def boot_loop(self) -> bool:
        with self._lock:
            self._expire_boots()
            return len(self._boot_times) >= self._loop_count

    def health(self) -> dict:
        loop = self.boot_loop
        with self._lock:
            return {
                "since": self.since,
                "boots": self.boots,
                "last_reset": self.last_reset,
                "panics": self.panics,
                "last_panic": self.last_panic,
                "boot_loop": loop,
            }

    def firmware(self) -> dict:
        with self._lock:
            return dict(self.fw)

    # ---------- internos ----------

    def _expire_boots(self) -> None:
        now = self._clock()
        while self._boot_times and now - self._boot_times[0] > self._loop_window:
            self._boot_times.popleft()

    def _line(self, line: str) -> bool:
        with self._lock:
            m = _RESET_RE.search(line)
            if m:
                reason = m.group(1)
                self.boots += 1
                self._boot_times.append(self._clock())
                bare = reason.replace("_", "")      # RTCWDT_BROWN_OUT_RESET → ...BROWNOUT...
                self.last_reset = {"ts": _now_iso(self._clock), "reason": reason,
                                   "abnormal": any(k in bare for k in _ABNORMAL_RESET)}
                return True
            for rx, kind in _PANIC_RES:
                m = rx.search(line)
                if m:
                    self.panics += 1
                    self.last_panic = {"ts": _now_iso(self._clock), "kind": kind,
                                       "detail": m.group(1) if m.groups() else None,
                                       "line": line.strip()[:200]}
                    return True
            for key, rx in _FW_RES.items():
                m = rx.search(line)
                if m and self.fw.get(key) != m.group(1):
                    self.fw[key] = m.group(1)
                    return True
        return False

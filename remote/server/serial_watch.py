#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serial_watch.py — SerialWatch: lee el serial del device línea a línea y saca
de ahí la salud del firmware, su identificación y los eventos de la placa.

Las líneas las corta DeviceLog (una sola tubería: las mismas líneas que van al
archivo, con su cursor): on_line(texto, cursor, ts). Acá se aplica el \r (queda
el último segmento) y se saca el ANSI.

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

Eventos (on_event(type, detail, cursor, ts), van a events.jsonl):
- boot: cada línea rst: (la ROM la imprime siempre, es el inicio del arranque)
- boot_loop: start al detectarlo / end cuando se estabiliza. Mientras está
  activo, los boot sueltos no se registran (un loop escribiría miles por hora).
  El end lleva ts = último boot + ventana (cuándo terminó de verdad) y el ts y
  cursor del último boot; se registra con la primera línea siguiente o con
  poll() (el proceso lo llama cada segundo: una placa muda también lo cierra).
- panic
- fw: solo si cambió (proyecto, versión o IDF), al ver la línea ESP-IDF del
  arranque o, si no aparece, en el siguiente rst:.
"""
import collections
import datetime as dt
import re
import threading
import time
from typing import Callable, Optional

from server import events, taglog

TAG = "serial_watch"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_RESET_RE = re.compile(r"rst:0x[0-9a-fA-F]+ \(([A-Z0-9_]+)\)")
_FW_RES = {
    "project": re.compile(r"(?:app_init|cpu_start): Project name:\s+(\S+)"),
    "version": re.compile(r"(?:app_init|cpu_start): App version:\s+(\S+)"),
    "idf": re.compile(r"(?:app_init|cpu_start): ESP-IDF:\s+(\S+)"),
}
# (regex, tipo). El primer grupo, si hay, va como detalle.
_PANIC_RES = [
    (re.compile(r"Guru Meditation Error: Core\s+\d+ panic'ed \(([^)]*)\)"), "guru"),
    (re.compile(r"abort\(\) was called"), "abort"),
    (re.compile(r"Brownout detector was triggered"), "brownout"),
    (re.compile(r"Task watchdog got triggered"), "task_wdt"),
    (re.compile(r"\*\*\*ERROR\*\*\* A stack overflow in task (\S+)"), "stack_overflow"),
    (re.compile(r"assert failed:"), "assert"),
]
# Motivos de reinicio que por sí solos ya indican un problema.
_ABNORMAL_RESET = ("WDT", "BROWNOUT", "PANIC")

MAX_LINE = 4096
# Boot loop: BOOT_LOOP_COUNT arranques en BOOT_LOOP_WINDOW segundos. Un firmware
# en loop reinicia cada 1-10 s; 5 en 60 s lo agarra y deja lugar a unos resets
# a mano. Antes era 3 en 120 s: flash + dos `reset --verify` ya era un "loop".
# Igual, los resets intencionales (flash, erase, Ctrl-T Ctrl-R/P del monitor:
# los `command` del api) ponen los contadores en cero (reset_counters).
BOOT_LOOP_COUNT = 5
BOOT_LOOP_WINDOW = 60.0   # segundos


def line_kind(line: str) -> Optional[str]:
    """"boot" (línea rst:) o "panic" para una línea ya limpia (sin ANSI, \r
    aplicado); None si no es ninguna. La misma detección que on_line: la usa
    logrange para buscar `--until boot|panic` en el log."""
    if _RESET_RE.search(line):
        return "boot"
    if any(rx.search(line) for rx, _ in _PANIC_RES):
        return "panic"
    return None


def _now_iso(clock: Callable[[], float]) -> str:
    return dt.datetime.fromtimestamp(clock()).isoformat(timespec="seconds")


class SerialWatch:

    def __init__(self, on_change: Optional[Callable[[], None]] = None,
                 clock: Callable[[], float] = time.time,
                 boot_loop_count: int = BOOT_LOOP_COUNT,
                 boot_loop_window: float = BOOT_LOOP_WINDOW,
                 on_event: Optional[Callable] = None):
        """on_event(type, detail, cursor, ts): DeviceLog.event en producción."""
        self._on_change = on_change
        self._on_event = on_event
        self._clock = clock
        self._loop_count = boot_loop_count
        self._loop_window = boot_loop_window
        self._lock = threading.Lock()
        self._loop_active = False
        self._loop_boots = 0
        self._last_boot_t = 0.0           # self._clock() del último boot (la ventana)
        self._last_boot = None            # {"ts", "cursor"} del último boot, para el end
        self._fw_dirty = False
        self._fw_at = (None, None)        # (cursor, ts) de la primera línea que cambió el fw
        self.boots = 0
        self.panics = 0
        self.last_reset: Optional[dict] = None
        self.last_panic: Optional[dict] = None
        self.fw: dict = {}
        self._boot_times = collections.deque()
        self.since = _now_iso(clock)

    def reset_counters(self) -> None:
        """Antes de un flash, un erase o un reset pedido al monitor (Ctrl-T
        Ctrl-R/P): reinician el chip a propósito, y no tienen que contar como
        problema. La info de firmware se conserva. Un
        boot loop en curso se da por terminado."""
        evs = []
        with self._lock:
            if self._loop_active:
                evs.append(self._end_loop(None, expired=False))
            self.boots = self.panics = 0
            self.last_reset = self.last_panic = None
            self._boot_times.clear()
            self.since = _now_iso(self._clock)
        self._emit(evs)

    # ---------- entrada ----------

    def on_line(self, text: str, cursor=None, ts: Optional[float] = None) -> None:
        """Una línea serial completa (sin \n), con el cursor de su inicio en el
        log y la hora en que llegó."""
        line = _ANSI_RE.sub("", text.rstrip("\r").rsplit("\r", 1)[-1])[:MAX_LINE]
        evs = []
        with self._lock:
            changed = self._line(line, cursor, ts, evs) if line else False
            if self._loop_active and not self._in_loop():
                evs.append(self._end_loop(cursor))
        self._emit(evs)
        if changed and self._on_change:
            self._on_change()

    def poll(self) -> bool:
        """Cierra un boot loop vencido aunque no lleguen líneas (placa muda).
        Devuelve True si lo cerró (hay que republicar la salud)."""
        evs = []
        with self._lock:
            if self._loop_active and not self._in_loop():
                evs.append(self._end_loop(None))
        self._emit(evs)
        return bool(evs)

    # ---------- salida ----------

    @property
    def boot_loop(self) -> bool:
        with self._lock:
            return self._in_loop()

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

    def _emit(self, evs: list) -> None:
        """Fuera del lock: on_event escribe a disco (DeviceLog.event)."""
        if self._on_event is None:
            return
        for type_, detail, cursor, ts in evs:
            try:
                self._on_event(type_, detail, cursor, ts)
            except Exception as e:      # un evento que no se pudo escribir no corta la lectura del serial
                taglog.debug(TAG, f"evento {type_} no registrado: {e}")

    def _expire_boots(self) -> None:
        now = self._clock()
        while self._boot_times and now - self._boot_times[0] > self._loop_window:
            self._boot_times.popleft()

    def _in_loop(self) -> bool:
        self._expire_boots()
        return len(self._boot_times) >= self._loop_count

    def _end_loop(self, cursor, expired: bool = True) -> tuple:
        """expired: el loop terminó solo → ts = último boot + ventana. Si no (lo
        corta un flash/erase), ts = ahora."""
        self._loop_active = False
        ts = self._last_boot_t + self._loop_window if expired else None
        return ("boot_loop", {"phase": "end", "boots": self._loop_boots, "last_boot": self._last_boot},
                cursor, ts)

    def _fw_event(self) -> tuple:
        self._fw_dirty = False
        cursor, ts = self._fw_at
        return ("fw", dict(self.fw), cursor, ts)

    def _line(self, line: str, cursor, ts, evs: list) -> bool:
        m = _RESET_RE.search(line)
        if m:
            if self._fw_dirty:              # firmware sin línea ESP-IDF: el fw sale antes del boot
                evs.append(self._fw_event())
            reason = m.group(1)
            self.boots += 1
            self._last_boot_t = self._clock()
            self._boot_times.append(self._last_boot_t)
            self._last_boot = {"ts": events.iso_ms(ts if ts is not None else self._last_boot_t),
                               # pre-MAC el cursor es una posición del buffer: no va al JSON
                               "cursor": cursor if isinstance(cursor, str) else None}
            bare = reason.replace("_", "")      # RTCWDT_BROWN_OUT_RESET → ...BROWNOUT...
            abnormal = any(k in bare for k in _ABNORMAL_RESET)
            self.last_reset = {"ts": _now_iso(self._clock), "reason": reason, "abnormal": abnormal}
            if self._loop_active:
                self._loop_boots += 1
            elif self._in_loop():
                self._loop_active = True
                self._loop_boots = len(self._boot_times)
                evs.append(("boot_loop", {"phase": "start", "boots": self._loop_boots}, cursor, ts))
            else:
                evs.append(("boot", {"reason": reason, "abnormal": abnormal}, cursor, ts))
            return True
        for rx, kind in _PANIC_RES:
            m = rx.search(line)
            if m:
                detail = m.group(1) if m.groups() else None
                self.panics += 1
                self.last_panic = {"ts": _now_iso(self._clock), "kind": kind,
                                   "detail": detail, "line": line.strip()[:200]}
                evs.append(("panic", {"kind": kind, "reason": detail, "line": line.strip()[:200]}, cursor, ts))
                return True
        for key, rx in _FW_RES.items():
            m = rx.search(line)
            if not m:
                continue
            changed = self.fw.get(key) != m.group(1)
            if changed:
                self.fw[key] = m.group(1)
                if not self._fw_dirty:
                    self._fw_dirty, self._fw_at = True, (cursor, ts)
            if key == "idf" and self._fw_dirty:
                evs.append(self._fw_event())
            return changed
        return False

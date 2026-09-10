#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
taglog.py — logging con TAG + timestamp para todo el server, estilo
ESP_LOGI/W/E/D(TAG, ...) de ESP-IDF.

Reemplaza los nprint() duplicados (remote_esp32.py, monitor.py, protocol.py,
flash.py definen cada uno el suyo) y el logging.getLogger ad hoc de cada
módulo por una sola interfaz: taglog.info(TAG, msg), taglog.warn(...),
taglog.error(...), taglog.debug(...). Un TAG estático por módulo, igual
que la convención de C/C++ del equipo.

Dónde termina escribiendo cada línea es responsabilidad de los sinks, no
de esta interfaz — por default solo hay un sink a stdout. Agregar un
archivo, JSON lines por device, lo que sea, es sumar un sink nuevo con
add_sink() sin tocar ningún call site. Migrar los módulos existentes a
usar esto es un paso aparte (no en este commit) — acá solo vive la
librería, ya conectada como consumidor de prueba en device.py.
"""
import datetime as dt
import threading
from typing import Callable

# (timestamp, level, tag, msg) -> None
Sink = Callable[[str, str, str, str], None]

_lock = threading.Lock()
_sinks: list = []


def _stdout_sink(ts: str, level: str, tag: str, msg: str) -> None:
    print(f"{ts} | {level:5s} | {tag:14s} | {msg}", flush=True)


def add_sink(sink: Sink) -> None:
    """Registra un sink adicional (archivo, jsonl, lo que sea). No reemplaza los existentes."""
    with _lock:
        _sinks.append(sink)


def reset_default_sinks() -> None:
    """Vuelve a dejar solo el sink de stdout. Para tests, o para reconfigurar sinks al vuelo."""
    with _lock:
        _sinks.clear()
        _sinks.append(_stdout_sink)


def clear_sinks() -> None:
    """Saca todos los sinks, incluido stdout. Para tests que quieren silencio total."""
    with _lock:
        _sinks.clear()


def _emit(level: str, tag: str, msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        sinks = list(_sinks)
    for sink in sinks:
        try:
            sink(ts, level, tag, msg)
        except Exception:
            pass  # un sink roto no debe tirar abajo el logging de los demás


def info(tag: str, msg: str) -> None:
    _emit("INFO", tag, msg)


def warn(tag: str, msg: str) -> None:
    _emit("WARN", tag, msg)


def error(tag: str, msg: str) -> None:
    _emit("ERROR", tag, msg)


def debug(tag: str, msg: str) -> None:
    _emit("DEBUG", tag, msg)


reset_default_sinks()

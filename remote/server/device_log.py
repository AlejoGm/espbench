#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
device_log.py — DeviceLog: dueño único de la escritura del log de un dispositivo.

Reemplaza el mecanismo actual (tmux pipe-pane escribiendo a ciegas por tty,
EspMonitor escribiendo por su cuenta a serial.log) por un solo escritor
explícito.

Antes de conocer la MAC del device (arranque del proceso, lectura de MAC
en curso), bufferea en memoria. `adopt(mac)` vuelca ese buffer al archivo
del device (paths.device_output_log) y de ahí en más escribe directo.

Todavía no está conectado a EspMonitor ni a protocol.py — eso es la
fase 3 del plan de refactor (mover el .stop()/.start() y el logging
duplicado nprint()+svc_log a esta interfaz).
"""
import pathlib
import threading
import time

from server import paths


class DeviceLog:
    """Log de un dispositivo. Una instancia por proceso (por tty)."""

    def __init__(self, tty_name: str):
        self.tty_name = tty_name
        self._lock = threading.Lock()
        self._mac: str | None = None
        self._buffer: list[str] = []
        self._fh = None

    @property
    def adopted(self) -> bool:
        return self._mac is not None

    def write(self, line: str) -> None:
        """Escribe una línea. Bufferea en memoria hasta que se llame adopt()."""
        with self._lock:
            if self._fh is None:
                self._buffer.append(line)
                return
            self._fh.write(line)
            self._fh.flush()

    def adopt(self, mac: str) -> None:
        """Promueve el log de 'bufereado por tty' a 'archivo del device'.

        Vuelca el buffer acumulado (con un separador que deja constancia de
        desde qué tty se adoptó) y abre el archivo del device en modo append,
        para no pisar historial de sesiones anteriores del mismo device.

        No-op si ya se adoptó antes (protege contra doble llamada).
        """
        with self._lock:
            if self._mac is not None:
                return
            self._mac = mac
            out_path = paths.device_output_log(mac)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = out_path.open("a", encoding="utf-8", errors="replace")
            if self._buffer:
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                self._fh.write(f"--- adoptado desde tty={self.tty_name} @ {ts} ---\n")
                for line in self._buffer:
                    self._fh.write(line)
                self._buffer.clear()
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

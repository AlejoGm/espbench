#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
device.py — TtyPort / Device / DeviceManager: modelo de objetos + FSM.

Fase 2 del refactor de arquitectura del server (ver memoria del proyecto).
Reemplaza el ida-y-vuelta implícito de hoy (_ignore_signals_flag,
mon.stop()/start() sueltos en protocol.py) por una máquina de estados
explícita, con transiciones que se pueden rechazar en vez de asumir que
"nunca pasa".

Todavía NO está conectado a EspMonitor ni a protocol.py — a propósito.
Esta fase solo construye y testea el modelo con fakes (ver tests/test_device.py).
Enchufarlo de verdad en remote_esp32.py/protocol.py es la fase 3.

No confundir con device_registry.py (DeviceRegistry/DeviceInfo) — ese es
el modelo de lectura que usa el dashboard hoy, basado en escanear archivos.
Este módulo es el modelo de escritura/orquestación que corre dentro del
proceso remote_esp32.py. Se conectan en la fase 5 (dashboard lee
devices/<mac>/state.json en vez de inferir).

Topología: 1 proceso remote_esp32.py = 1 TtyPort = 1 Device, para toda la
vida del proceso (ver decisión de arquitectura: no se consolida a un
servicio único). Por eso el TtyPort se fija en el constructor de Device y
no hay un "attach()" para cambiarlo — "enchufar" pasa una sola vez, al
crear el Device; "desenchufar" (disconnect()) es terminal.
"""
import dataclasses
import enum
import pathlib
import threading

from server.device_log import DeviceLog


class DeviceState(enum.Enum):
    DISCOVERING = "discovering"    # arrancó el proceso, leyendo MAC
    MONITORING = "monitoring"      # MAC conocida (o UNKNOWN), EspMonitor activo
    FLASHING = "flashing"          # flasheo en curso, monitor pausado
    ERASING = "erasing"            # erase_region interactivo, monitor pausado
    UNKNOWN = "unknown"            # nunca se pudo leer la MAC — funciona igual, sin identidad
    DISCONNECTED = "disconnected"  # tty desapareció — terminal


class InvalidTransition(Exception):
    """Se pidió una transición que la FSM no permite desde el estado actual."""


@dataclasses.dataclass(frozen=True)
class TtyPort:
    """Puerto físico. Tty path + puerto TCP de control. Sin identidad de device."""
    tty_path: str
    tcp_port: int

    @property
    def tty_name(self) -> str:
        return pathlib.Path(self.tty_path).name

    @classmethod
    def from_tty_path(cls, tty_path: str, base_port: int = 5000) -> "TtyPort":
        name = pathlib.Path(tty_path).name
        suffix = name.replace("ttyUSB", "")
        try:
            n = int(suffix)
        except ValueError:
            n = 0
        return cls(tty_path=tty_path, tcp_port=base_port + n)


class Device:
    """
    Device lógico enchufado a un único TtyPort para toda la vida del proceso.

    Arranca sin identidad (DISCOVERING) y se promueve a MONITORING apenas
    se conoce la MAC (promote()) o a UNKNOWN si nunca se pudo leer
    (mark_unknown()) — un device UNKNOWN puede promoverse más tarde si la
    MAC se resuelve tarde (fallback por serial output).

    No ejecuta nada de esptool/esp_idf_monitor por su cuenta — solo valida
    transiciones y delega el log en DeviceLog. Quien haga el trabajo real
    (EspMonitor, protocol.py) se conecta en la fase 3.
    """

    def __init__(self, tty_port: TtyPort, device_log: DeviceLog):
        self.tty_port = tty_port
        self.device_log = device_log
        self.mac: str | None = None
        self.state = DeviceState.DISCOVERING
        self._lock = threading.Lock()

    @property
    def tty_name(self) -> str:
        return self.tty_port.tty_name

    def _require(self, *allowed: DeviceState, action: str) -> None:
        if self.state not in allowed:
            raise InvalidTransition(
                f"{action}: estado actual es {self.state.value}, "
                f"se esperaba uno de {[s.value for s in allowed]}"
            )

    def promote(self, mac: str) -> None:
        """DISCOVERING|UNKNOWN → MONITORING. Adopta el log a esta MAC."""
        with self._lock:
            self._require(DeviceState.DISCOVERING, DeviceState.UNKNOWN, action="promote")
            self.mac = mac
            self.device_log.adopt(mac)
            self.state = DeviceState.MONITORING

    def mark_unknown(self) -> None:
        """DISCOVERING → UNKNOWN. La MAC no se pudo leer (esptool ni serial)."""
        with self._lock:
            self._require(DeviceState.DISCOVERING, action="mark_unknown")
            self.state = DeviceState.UNKNOWN

    def start_flash(self) -> None:
        """MONITORING → FLASHING. Rechaza flashear si ya está flasheando/borrando."""
        with self._lock:
            self._require(DeviceState.MONITORING, action="start_flash")
            self.state = DeviceState.FLASHING

    def finish_flash(self) -> None:
        """FLASHING → MONITORING."""
        with self._lock:
            self._require(DeviceState.FLASHING, action="finish_flash")
            self.state = DeviceState.MONITORING

    def start_erase(self) -> None:
        """MONITORING → ERASING."""
        with self._lock:
            self._require(DeviceState.MONITORING, action="start_erase")
            self.state = DeviceState.ERASING

    def finish_erase(self) -> None:
        """ERASING → MONITORING."""
        with self._lock:
            self._require(DeviceState.ERASING, action="finish_erase")
            self.state = DeviceState.MONITORING

    def disconnect(self) -> None:
        """Cualquier estado → DISCONNECTED. Terminal e idempotente."""
        with self._lock:
            if self.state == DeviceState.DISCONNECTED:
                return
            self.state = DeviceState.DISCONNECTED
            self.device_log.close()


class DeviceManager:
    """
    Arma TtyPort + Device + DeviceLog para un tty y corre el descubrimiento
    de MAC. Vive dentro del proceso remote_esp32.py — no es un servicio
    separado (ver decisión de arquitectura).

    mac_reader es inyectado a propósito: en producción (fase 3) sería algo
    como `lambda: flash.read_mac(tty_path)`. Acá permite testear la FSM
    sin esptool ni hardware.
    """

    def __init__(self, tty_path: str, mac_reader):
        self.tty_port = TtyPort.from_tty_path(tty_path)
        self.device = Device(self.tty_port, DeviceLog(self.tty_port.tty_name))
        self._mac_reader = mac_reader

    def discover(self) -> None:
        """Intenta leer la MAC una vez. Promueve o marca UNKNOWN."""
        mac = self._mac_reader()
        if mac:
            self.device.promote(mac)
        else:
            self.device.mark_unknown()

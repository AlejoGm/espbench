#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
device.py — TtyPort / Device / DeviceManager: modelo de objetos + FSM.

Reemplaza el ida-y-vuelta implícito que había (_ignore_signals_flag,
mon.stop()/start() sueltos en protocol.py) por una máquina de estados
explícita, con transiciones que se rechazan en vez de asumir que "nunca pasa".

    DISCOVERING ──MAC──> MONITORING ⇄ FLASHING
         │                    ⇅
         │ (sin MAC)        ERASING
         ▼
      UNKNOWN ──MAC tarde──> MONITORING
         ⇅
     FLASHING / ERASING   (un device con flash encryption no siempre deja
                           leer la MAC, y tiene que poder flashearse igual)

    cualquiera ──tty desaparece──> DISCONNECTED (terminal)

FLASHING y ERASING vuelven al estado del que salieron (MONITORING o UNKNOWN),
salvo que la MAC se haya resuelto mientras tanto: ahí vuelven a MONITORING.

Topología: 1 proceso remote_esp32.py = 1 TtyPort = 1 Device, para toda la vida
del proceso. No se consolida en un servicio único (ver docs/ARCHITECTURE.md):
tmux por proceso da aislamiento de crash, limpieza de recursos al morir y
reset por device sin costo.

Cada transición se loguea por taglog y se publica en run/<tty>.json
(runstate.py), que es como el dashboard — otro proceso — se entera del estado.
"""
import dataclasses
import datetime as dt
import enum
import os
import pathlib
import threading
import time
from typing import Callable, Optional

from server import runstate, taglog
from server.device_log import DeviceLog
from server.serial_watch import SerialWatch

TAG = "device"


class DeviceState(enum.Enum):
    DISCOVERING = "discovering"    # arrancó el proceso, leyendo MAC
    MONITORING = "monitoring"      # MAC conocida, EspMonitor activo
    FLASHING = "flashing"          # flasheo en curso, monitor pausado
    ERASING = "erasing"            # erase_region interactivo, monitor pausado
    UNKNOWN = "unknown"            # MAC no leída — monitor activo, sin identidad
    DISCONNECTED = "disconnected"  # tty desapareció — terminal


# Estados en los que el puerto serie está tomado por esptool: una señal de
# terminación en el medio puede dejar el chip a medio escribir.
BUSY_STATES = (DeviceState.FLASHING, DeviceState.ERASING)


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
        """Deriva el puerto de ttyUSB<N>. Solo para tests/uso exploratorio: en
        producción el puerto lo decide la capa de infra (esp32_tmux.sh) y llega
        explícito por --control-port."""
        name = pathlib.Path(tty_path).name
        try:
            n = int(name.replace("ttyUSB", ""))
        except ValueError:
            n = 0
        return cls(tty_path=tty_path, tcp_port=base_port + n)


class Device:
    """Device lógico enchufado a un único TtyPort para toda la vida del proceso."""

    def __init__(self, tty_port: TtyPort, device_log: DeviceLog,
                 state_sink: Optional[Callable[[dict], None]] = None,
                 watch: Optional[SerialWatch] = None):
        self.tty_port = tty_port
        self.device_log = device_log
        self.watch = watch
        self.mac: Optional[str] = None
        self.state = DeviceState.DISCOVERING
        self._resume_state: Optional[DeviceState] = None
        self._state_sink = state_sink
        self._lock = threading.RLock()
        self._publish()

    @property
    def tty_name(self) -> str:
        return self.tty_port.tty_name

    @property
    def busy(self) -> bool:
        return self.state in BUSY_STATES

    def publish(self) -> None:
        """Republicar el estado sin transición (cambió la salud o el firmware)."""
        with self._lock:
            self._publish()

    def snapshot(self) -> dict:
        log_path = self.device_log.path
        snap = {
            "tty": self.tty_name,
            "tty_path": self.tty_port.tty_path,
            "tcp_port": self.tty_port.tcp_port,
            "mac": self.mac,
            "state": self.state.value,
            "log_path": str(log_path) if log_path else None,
            "pid": os.getpid(),
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        if self.watch is not None:
            snap["health"] = self.watch.health()
            snap["fw"] = self.watch.firmware()
        return snap

    # ---------- internos ----------

    def _who(self) -> str:
        return f"{self.tty_name} (mac={self.mac or '?'})"

    def _require(self, *allowed: DeviceState, action: str) -> None:
        if self.state not in allowed:
            msg = (f"{action}: estado actual es {self.state.value}, "
                   f"se esperaba uno de {[s.value for s in allowed]}")
            taglog.warn(TAG, f"{self._who()}: transición rechazada — {msg}")
            raise InvalidTransition(msg)

    def _set_state(self, new: DeviceState) -> None:
        old = self.state
        self.state = new
        taglog.info(TAG, f"{self._who()}: {old.value} -> {new.value}")
        self._publish()

    def _publish(self) -> None:
        if self._state_sink is None:
            return
        try:
            self._state_sink(self.snapshot())
        except Exception as e:
            # Que no se pueda publicar el estado no puede tirar abajo un flash.
            taglog.error(TAG, f"{self._who()}: no se pudo publicar el estado: {e}")

    def _start_busy(self, busy: DeviceState, action: str) -> None:
        with self._lock:
            self._require(DeviceState.MONITORING, DeviceState.UNKNOWN, action=action)
            self._resume_state = self.state
            if self.watch is not None:
                self.watch.reset_counters()   # el flash/erase reinicia el chip a propósito
            self._set_state(busy)

    def _finish_busy(self, busy: DeviceState, action: str) -> None:
        with self._lock:
            self._require(busy, action=action)
            resume = self._resume_state or DeviceState.MONITORING
            self._resume_state = None
            self._set_state(resume)

    # ---------- transiciones ----------

    def promote(self, mac: str) -> None:
        """Se conoció la MAC. DISCOVERING|UNKNOWN → MONITORING; si llega en medio
        de un FLASHING/ERASING el estado no cambia y se vuelve a MONITORING al
        terminar. Adopta el log a devices/<mac>/."""
        with self._lock:
            if self.mac is not None:
                taglog.warn(TAG, f"{self._who()}: promote({mac}) rechazado — ya tiene MAC")
                raise InvalidTransition(f"promote: ya tiene MAC {self.mac}")
            self._require(DeviceState.DISCOVERING, DeviceState.UNKNOWN,
                          DeviceState.FLASHING, DeviceState.ERASING, action="promote")
            self.mac = mac
            self.device_log.adopt(mac)
            if self.busy:
                self._resume_state = DeviceState.MONITORING
                taglog.info(TAG, f"{self._who()}: MAC resuelta durante {self.state.value}")
                self._publish()
            else:
                self._set_state(DeviceState.MONITORING)

    def mark_unknown(self) -> None:
        """DISCOVERING → UNKNOWN. El log pasa al hogar provisorio por tty."""
        with self._lock:
            self._require(DeviceState.DISCOVERING, action="mark_unknown")
            self.device_log.adopt_unknown()
            self._set_state(DeviceState.UNKNOWN)

    def start_flash(self) -> None:
        self._start_busy(DeviceState.FLASHING, "start_flash")

    def finish_flash(self) -> None:
        self._finish_busy(DeviceState.FLASHING, "finish_flash")

    def start_erase(self) -> None:
        self._start_busy(DeviceState.ERASING, "start_erase")

    def finish_erase(self) -> None:
        self._finish_busy(DeviceState.ERASING, "finish_erase")

    def disconnect(self) -> None:
        """Cualquier estado → DISCONNECTED. Terminal e idempotente."""
        with self._lock:
            if self.state == DeviceState.DISCONNECTED:
                return
            self._set_state(DeviceState.DISCONNECTED)
            self.device_log.close()


class DeviceManager:
    """
    Arma TtyPort + Device + DeviceLog para un tty y corre el descubrimiento de
    MAC. Vive dentro del proceso remote_esp32.py — no es un servicio aparte.

    mac_reader y las dependencias de tiempo/filesystem son inyectables: en
    producción mac_reader es `lambda: flash.read_mac(tty_path)`; en tests, un fake.
    """

    def __init__(self, tty_path: str, mac_reader: Callable[[], Optional[str]],
                 tcp_port: Optional[int] = None,
                 state_sink: Optional[Callable[[dict], None]] = None,
                 publish_state: bool = True):
        self.tty_port = (
            TtyPort(tty_path=tty_path, tcp_port=tcp_port)
            if tcp_port is not None
            else TtyPort.from_tty_path(tty_path)
        )
        if state_sink is None and publish_state:
            tty_name = self.tty_port.tty_name
            state_sink = lambda snap: runstate.write(tty_name, snap)  # noqa: E731
        self.watch = SerialWatch()
        self.device = Device(self.tty_port, DeviceLog(self.tty_port.tty_name),
                             state_sink=state_sink, watch=self.watch)
        self.watch._on_change = self.device.publish
        self._mac_reader = mac_reader

    def on_serial(self, data: bytes) -> None:
        """Sink del EspMonitor: el serial va al log del device y al SerialWatch."""
        self.device.device_log.write_bytes(data)
        self.watch.feed(data)

    def discover(self, attempts: int = 1, delay: float = 0.0,
                 sleep: Callable[[float], None] = time.sleep) -> bool:
        """Lee la MAC hasta `attempts` veces (el chip puede no estar listo justo
        después de que udev crea el tty). Promueve o marca UNKNOWN."""
        for i in range(attempts):
            mac = self._mac_reader()
            if mac:
                self.device.promote(mac)
                return True
            if i < attempts - 1:
                taglog.info(TAG, f"{self.device.tty_name}: MAC no leída, reintento en {delay:g}s")
                sleep(delay)
        taglog.warn(TAG, f"{self.device.tty_name}: esptool no pudo leer la MAC")
        self.device.mark_unknown()
        return False

    def resolve_mac_from_output(self, get_output: Callable[[], str],
                                parse: Callable[[str], Optional[str]],
                                timeout: float = 15.0, poll: float = 0.5,
                                clock: Callable[[], float] = time.monotonic,
                                sleep: Callable[[float], None] = time.sleep) -> bool:
        """Fallback para cuando esptool no puede leer la MAC (flash encryption):
        busca la MAC que imprime el firmware al bootear."""
        deadline = clock() + timeout
        while clock() < deadline:
            if self.device.mac is not None:
                return True
            found = parse(get_output())
            if found:
                try:
                    self.device.promote(found)
                    taglog.info(TAG, f"{self.device.tty_name}: MAC leída desde serial: {found}")
                    return True
                except InvalidTransition:
                    return self.device.mac is not None
            sleep(poll)
        taglog.warn(TAG, f"{self.device.tty_name}: no se pudo leer la MAC desde serial")
        return False

    def watch_tty(self, stop: threading.Event,
                  exists: Callable[[str], bool] = os.path.exists,
                  poll: float = 1.0) -> bool:
        """Bloquea hasta que el tty desaparezca (→ DISCONNECTED, devuelve True)
        o hasta que se pida parar (devuelve False)."""
        while not stop.is_set():
            if not exists(self.tty_port.tty_path):
                taglog.warn(TAG, f"{self.device.tty_name}: el tty desapareció")
                self.device.disconnect()
                return True
            stop.wait(poll)
        return False

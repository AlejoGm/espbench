#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
remote_esp32.py — un proceso por dispositivo: monitor serial persistente +
servidor TCP de flasheo remoto.

Lo lanza esp32_tmux.sh en una sesión tmux por device. Arma el modelo
(DeviceManager → TtyPort + Device + DeviceLog), identifica el device por su
MAC y levanta tres hilos alrededor:

- control_server: pedidos de flash/unlock por TCP (protocol.py)
- MAC por serial: si esptool no pudo leerla al arrancar (flash encryption),
  la busca en lo que imprime el firmware al bootear
- watcher del tty: si el puerto desaparece, DISCONNECTED y el proceso termina
  (esp32_tmux.sh relanza la sesión cuando vuelve a aparecer)

Logs: todo pasa por taglog → stdout (la sesión tmux, `devremote <N>`) y el
DeviceLog del device (devices/<mac>/output.log, lo que muestra el dashboard).
"""
import argparse
import os
import pathlib
import signal
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from common import mac_to_sn_sfy
from server import auth, locks, paths, runstate, taglog
from server.device import Device, DeviceManager, DeviceState
from server.device_registry import DevicesFile
from server.erase import erase_region_interactive
from server.flash import parse_mac_from_serial, read_mac
from server.monitor import EspMonitor
from server.protocol import control_server

TAG = "remote_esp32"

MAC_READ_ATTEMPTS = 3
MAC_READ_DELAY = 3.0       # el chip puede no estar listo justo después de que udev crea el tty
MAC_SERIAL_TIMEOUT = 15.0

_shutdown = threading.Event()
_device: "Device | None" = None


def _on_signal(signum, frame):
    # Durante un flash o un erase el puerto está en manos de esptool: cortarlo
    # en el medio puede dejar el chip a medio escribir.
    if _device is not None and _device.busy:
        taglog.warn(TAG, f"señal {signum} ignorada: {_device.state.value} en curso")
        return
    taglog.info(TAG, f"señal {signum}: terminando")
    _shutdown.set()


def register_mac(mac: str) -> None:
    """Alta en devices.json (MAC → nombre amigable). No pisa un nombre existente."""
    try:
        sn = mac_to_sn_sfy(mac)
        DevicesFile().register_mac(mac, sn)
        taglog.info(TAG, f"MAC {mac} (SN {sn}) registrada en devices.json")
    except Exception as e:
        taglog.warn(TAG, f"no se pudo registrar {mac} en devices.json: {e}")


def drop_foreign_reservation(device: Device) -> None:
    """Una reserva de este tty hecha para otra placa (los ttyUSB se renumeraron
    en un replug) no vale: esp32_tmux.sh la conservó, acá se borra."""
    dropped = locks.drop_if_other_board(device.tty_name, device.mac)
    if dropped is not None:
        taglog.warn(TAG, f"reserva de '{dropped.user}' borrada: era para la placa {dropped.mac}, "
                         f"en {device.tty_name} está {device.mac}")


def elf_for(device: Device):
    """El .elf del último flash exitoso, para decodificar backtraces."""
    if device.mac:
        candidate = paths.device_current_elf(device.mac)
        if candidate.exists():
            return candidate
    return paths.current_elf_file(device.tty_name)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Monitor persistente con esp_idf_monitor + flasheo remoto")
    ap.add_argument("-p", "--port-tty", required=True, help="Ruta del tty (ej: /dev/ttyUSB0 o /dev/esp-slot3)")
    ap.add_argument("-b", "--serial-baud", type=int, default=115200, help="Baudrate del firmware")
    ap.add_argument("-tcp", "--control-port", type=int, default=5000, help="Puerto TCP de control")
    ap.add_argument("--chip", default="auto")
    ap.add_argument("--flash-baud", type=int, default=921600)
    ap.add_argument("--token", default="")
    ap.add_argument("--base", default="/opt/esp")
    return ap.parse_args(argv)


def main(argv=None):
    global _device
    args = parse_args(argv)
    os.environ["ESP_BASE"] = args.base   # todo lo que lea paths.py en este proceso

    manager = DeviceManager(args.port_tty, mac_reader=lambda: read_mac(args.port_tty),
                            tcp_port=args.control_port)
    device = _device = manager.device
    taglog.add_sink(device.device_log.taglog_sink)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _on_signal)

    token_src = "--token" if args.token else ("api_token" if auth.read_token() else "no")
    taglog.info(TAG, f"inicio: tty={args.port_tty} tcp={args.control_port} chip={args.chip} "
                     f"baud={args.serial_baud}/{args.flash_baud} token={token_src} "
                     f"base={paths.esp_base()}")

    # MAC con esptool antes de arrancar el monitor: el puerto tiene que estar libre.
    if manager.discover(attempts=MAC_READ_ATTEMPTS, delay=MAC_READ_DELAY):
        register_mac(device.mac)
        drop_foreign_reservation(device)

    cfg = {"port": args.control_port, "tty": args.port_tty, "chip": args.chip,
           "flash_baud": args.flash_baud, "token": args.token}
    mon = EspMonitor(args.port_tty, args.serial_baud,
                     output_sink=manager.on_serial,
                     elf_path=lambda: elf_for(device),
                     on_ctrl_e=lambda: erase_region_interactive(mon, cfg, device))
    mon.start()

    if device.mac is None:
        def _mac_from_serial():
            if manager.resolve_mac_from_output(mon.get_recent_output, parse_mac_from_serial,
                                               timeout=MAC_SERIAL_TIMEOUT):
                register_mac(device.mac)
        threading.Thread(target=_mac_from_serial, daemon=True).start()

    threading.Thread(target=control_server, args=(cfg, mon, device), daemon=True).start()

    stop_watch = threading.Event()

    def _watch():
        if manager.watch_tty(stop_watch):
            _shutdown.set()
    threading.Thread(target=_watch, daemon=True).start()

    taglog.info(TAG, "listo — Ctrl-C para salir, Ctrl-E para Erase Region")
    try:
        while not _shutdown.is_set():
            _shutdown.wait(timeout=1.0)
            manager.tick()
    except KeyboardInterrupt:
        _shutdown.set()
    finally:
        stop_watch.set()
        mon.stop()
        if device.state == DeviceState.DISCONNECTED:
            # Se deja run/<tty>.json en "disconnected": esp32_tmux.sh lo usa para
            # saber que puede recrear la sesión cuando el tty vuelva.
            taglog.info(TAG, "fin (tty desconectado)")
        else:
            taglog.info(TAG, "fin")
            device.device_log.close()
            runstate.remove(device.tty_name)


if __name__ == "__main__":
    main()

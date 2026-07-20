#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
remote_esp32.py — Monitor persistente con esp_idf_monitor + flasheo remoto

Entrypoint delgado: gestión de señales, logging, arranque del monitor y del
servidor TCP de control (implementado en protocol.py).
"""

import argparse, datetime as dt, logging, os, pathlib, signal, sys, threading, time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from common import mac_to_sn_sfy
from device_registry import DevicesFile
from flash import read_mac, parse_mac_from_serial
from monitor import EspMonitor, _ignore_signals_flag
from protocol import control_server, ensure_dir

# ========== Bandera global de terminación ==========
_shutdown_flag = threading.Event()


def nprint(s):
    print(s + "\r\n", flush=True)


def normalize_line_endings(data: bytes) -> bytes:
    """
    Normaliza los saltos de línea para terminal:
    - Mantiene \\r\\n existentes
    - Convierte \\n solos a \\r\\n
    """
    result = data.replace(b'\r\n', b'\n')
    result = result.replace(b'\n', b'\r\n')
    return result


# ========== Señales ==========

def signal_handler(signum, frame):
    if _ignore_signals_flag.is_set():
        print(f"\r\n[DEBUG] Señal {signum} ignorada (operación temporal en curso)\r\n", flush=True)
        return
    print(f"\r\n[DEBUG] Señal recibida: {signum}\r\n", flush=True)
    print(f"[DEBUG] PID del proceso: {os.getpid()}\r\n", flush=True)
    _shutdown_flag.set()


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGHUP, signal_handler)


# ========== Utilidades de logging ==========

def setup_logging(logs_dir: pathlib.Path) -> logging.Logger:
    ensure_dir(logs_dir)
    service_log = logs_dir / "remote_esp32.service.log"
    L = logging.getLogger("svc")
    L.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout); sh.setFormatter(fmt); L.addHandler(sh)
    fh = logging.FileHandler(service_log, encoding="utf-8"); fh.setFormatter(fmt); L.addHandler(fh)
    return L


def day_log_path(base: pathlib.Path) -> pathlib.Path:
    daydir = base / dt.datetime.now().strftime("%Y%m%d")
    ensure_dir(daydir)
    return daydir / "serial.log"


# ========== main ==========

def main():
    ap = argparse.ArgumentParser(description="Monitor persistente con esp_idf_monitor + flasheo remoto")
    ap.add_argument("-p", "--port-tty", required=True, help="Ruta del /dev/ttyUSBx (ej: /dev/ttyUSB0)")
    ap.add_argument("-b", "--serial-baud", type=int, default=115200, help="Baudrate del firmware")
    ap.add_argument("-tcp", "--control-port", type=int, default=5000, help="Puerto TCP de control")
    ap.add_argument("--chip", default="auto")
    ap.add_argument("--flash-baud", type=int, default=921600)
    ap.add_argument("--token", default="")
    ap.add_argument("--base", default="/opt/esp")
    args = ap.parse_args()

    nprint("=" * 60)
    nprint("remote_esp32.py - Monitor + Flasheo Remoto")
    nprint("=" * 60)

    base = pathlib.Path(args.base)
    logs_dir = base / "logs"
    jobs_dir = base / "jobs"

    nprint(f"[main] directorio base: {base}")
    nprint(f"[main] logs: {logs_dir}")
    nprint(f"[main] jobs: {jobs_dir}")

    tty_name = os.path.basename(args.port_tty)
    tty_log_dir = logs_dir / tty_name
    ensure_dir(logs_dir)
    ensure_dir(jobs_dir)
    ensure_dir(tty_log_dir)

    svc_log = setup_logging(logs_dir)
    svc_log.info("========== INICIO DEL SERVICIO ==========")
    svc_log.info(f"TTY: {args.port_tty}")
    svc_log.info(f"Baud serial: {args.serial_baud}")
    svc_log.info(f"Puerto TCP: {args.control_port}")
    svc_log.info(f"Chip: {args.chip}")
    svc_log.info(f"Baud flash: {args.flash_baud}")
    svc_log.info(f"Token: {'configurado' if args.token else 'sin token'}")

    def _register_mac(mac: str):
        mac_file = tty_log_dir / "mac"
        mac_file.write_text(mac)
        try:
            mac_file.chmod(0o666)
        except Exception:
            pass
        try:
            sn = mac_to_sn_sfy(mac)
            DevicesFile().register_mac(mac, sn)
            svc_log.info(f"[mac] SN: {sn} — registrado en devices.json")
        except Exception as e:
            svc_log.warning(f"[mac] error en devices.json: {e}")

    # Leer MAC con esptool antes de arrancar el monitor (puerto libre).
    # Reintenta hasta 3 veces con 3s de espera: el chip puede no estar listo
    # inmediatamente después de que udev crea /dev/ttyUSBN.
    svc_log.info("[mac] leyendo MAC del dispositivo...")
    mac_addr = None
    for _attempt in range(3):
        mac_addr = read_mac(args.port_tty)
        if mac_addr:
            break
        if _attempt < 2:
            svc_log.info("[mac] reintentando en 3s...")
            time.sleep(3)
    mac_file = tty_log_dir / "mac"
    if mac_addr:
        svc_log.info(f"[mac] MAC: {mac_addr}")
        _register_mac(mac_addr)
    else:
        svc_log.warning("[mac] esptool no pudo leer MAC — se intentará desde serial output al bootear")
        if mac_file.exists():
            try:
                mac_file.unlink()
            except Exception:
                pass

    cfg = {
        "port": args.control_port,
        "tty": args.port_tty,
        "chip": args.chip,
        "flash_baud": args.flash_baud,
        "token": args.token,
        "logs_dir": logs_dir,
        "jobs_dir": jobs_dir,
        "base": base
    }

    elf_path = base / f"current_{os.path.basename(args.port_tty)}.elf"
    mon = EspMonitor(args.port_tty, args.serial_baud, logs_dir, elf_path=elf_path, cfg=cfg, svc_log=svc_log)
    svc_log.info("Iniciando monitor serial...\r\n")
    mon.start()

    if not mac_addr:
        def _mac_from_serial():
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                found = parse_mac_from_serial(mon.get_recent_output())
                if found:
                    svc_log.info(f"[mac] MAC leída desde serial: {found}")
                    _register_mac(found)
                    return
                time.sleep(0.5)
            buf = mon.get_recent_output()
            svc_log.warning("[mac] no se pudo leer MAC desde serial output")
            svc_log.warning(f"[mac] últimos 300 chars del buffer: {repr(buf[-300:])}")
        threading.Thread(target=_mac_from_serial, daemon=True).start()

    svc_log.info("Iniciando servidor de control TCP...\r\n")
    th = threading.Thread(target=control_server, args=(cfg, mon, svc_log), daemon=True)
    th.start()
    svc_log.info("Servidor TCP iniciado en hilo daemon\r\n")

    nprint("[main] Sistema listo. Presiona Ctrl-C para salir.")
    nprint("[main] Presiona Ctrl-E para entrar en modo Erase Region")
    try:
        while not _shutdown_flag.is_set():
            _shutdown_flag.wait(timeout=1.0)
    except KeyboardInterrupt:
        nprint("[main] Ctrl-C recibido, cerrando...")
        svc_log.info("Señal de interrupción recibida\r\n")
        _shutdown_flag.set()
    finally:
        nprint("[main] Señal de terminación recibida, cerrando...")
        svc_log.info("Señal de terminación recibida\r\n")
        svc_log.info("Deteniendo monitor serial...")
        mon.stop()
        svc_log.info("========== FIN DEL SERVICIO ==========\r\n")
        nprint("[main] Servicio detenido")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor.py — Monitor serial basado en esp_idf_monitor para ESP32

EspMonitor reemplaza a PicocomMonitor lanzando esp_idf_monitor en lugar de picocom,
con soporte de ELF para decodificación de backtraces.
"""

import logging, os, pathlib, select, shlex, signal, subprocess, sys, threading, time, pty, tty, termios

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from flash import find_esptool_cmd, run_cmd

# Bandera compartida para ignorar señales durante operaciones temporales (erase_region, etc.)
# Se importa en remote_esp32.py para que el signal_handler pueda usarla.
_ignore_signals_flag = threading.Event()

# ========== Constantes ==========
PTY_READ_SIZE = 4096
STDIN_READ_SIZE = 1024
SELECT_TIMEOUT = 0.1
TIMEOUT_PROCESS_TERMINATE = 3
TIMEOUT_THREAD_JOIN = 2
MONITOR_RESTART_DELAY = 0.8


def nprint(s):
    print(s + "\r\n", flush=True)


def normalize_line_endings(data: bytes) -> bytes:
    result = data.replace(b'\r\n', b'\n')
    result = result.replace(b'\n', b'\r\n')
    return result


# ========== Funciones para erase region ==========

def parse_partition_table(text: str) -> list[dict]:
    """
    Parsea una tabla de particiones del ESP32 desde texto del bootloader.
    Formato esperado:
    I (71) boot:  0 nvs              WiFi data        01 02 00012000 00100000

    Retorna lista de diccionarios con: name, type, subtype, offset, size
    """
    import re
    partitions = []

    # Patrón para el formato del bootloader del ESP32:
    # I (XX) boot:  N nombre          descripción     TT SS OOOOOOOO LLLLLLLL
    # Ejemplo: I (71) boot:  0 nvs              WiFi data        01 02 00012000 00100000
    # El patrón busca: I (número) boot: número nombre [descripción con espacios] tipo subtipo offset length
    pattern = r'I\s*\(\s*\d+\s*\)\s+boot:\s+\d+\s+(\w+)\s+[^\d]+\s+([0-9a-fA-F]{2})\s+([0-9a-fA-F]{2})\s+([0-9a-fA-F]{8})\s+([0-9a-fA-F]{8})'

    # También intentar formato CSV alternativo (por si acaso)
    pattern_csv = r'(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*([^,]*)\s*,\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)'

    in_partition_table = False

    for line in text.split('\n'):
        line_stripped = line.strip()

        # Detectar inicio de tabla de particiones
        if 'partition table' in line.lower() or '## Label' in line:
            in_partition_table = True
            continue

        # Detectar fin de tabla
        if 'end of partition table' in line.lower():
            in_partition_table = False
            continue

        if not in_partition_table and not line_stripped:
            continue

        # Intentar formato bootloader primero (más común)
        match = re.search(pattern, line)
        if match:
            name, ptype, subtype, offset_str, size_str = match.groups()
            try:
                offset = int(offset_str, 16)
                size = int(size_str, 16)
                partitions.append({
                    'name': name.strip(),
                    'type': ptype.strip(),
                    'subtype': subtype.strip(),
                    'offset': offset,
                    'offset_hex': f"0x{offset_str}",
                    'size': size,
                    'size_hex': f"0x{size_str}"
                })
                continue
            except ValueError:
                pass

        # Intentar formato CSV como fallback
        if not match:
            match_csv = re.search(pattern_csv, line)
            if match_csv:
                name, ptype, subtype, flags, offset_str, size_str = match_csv.groups()
                try:
                    # Remover 0x si está presente
                    offset_clean = offset_str.replace('0x', '').replace('0X', '')
                    size_clean = size_str.replace('0x', '').replace('0X', '')
                    offset = int(offset_clean, 16)
                    size = int(size_clean, 16)
                    partitions.append({
                        'name': name.strip(),
                        'type': ptype.strip(),
                        'subtype': subtype.strip(),
                        'offset': offset,
                        'offset_hex': offset_str if offset_str.startswith('0x') else f"0x{offset_clean}",
                        'size': size,
                        'size_hex': size_str if size_str.startswith('0x') else f"0x{size_clean}"
                    })
                except ValueError:
                    continue

    return partitions


def erase_region_interactive(mon: 'EspMonitor', cfg: dict, svc_log: logging.Logger):
    """
    Modo interactivo para borrar regiones de la flash.
    Detecta tabla de particiones o permite entrada manual.
    """
    nprint("\r\n" + "="*60)
    nprint("[erase] Modo Erase Region activado")
    nprint("="*60)

    # Obtener salida reciente del monitor para buscar tabla de particiones
    recent_output = mon.get_recent_output()
    partitions = parse_partition_table(recent_output)

    if partitions:
        nprint(f"\r\n[erase] Tabla de particiones detectada ({len(partitions)} particiones):\r\n")
        for i, p in enumerate(partitions, 1):
            nprint(f"  {i}. {p['name']:20s} @ {p['offset_hex']:>10s} ({p['size_hex']:>10s} bytes)")
        nprint("\r\n[erase] Selecciona particiones a borrar (ej: 1,3,5 o 'all' para todas):")
        nprint("[erase] O presiona Enter para entrada manual: ")

        # Restaurar stdin temporalmente para entrada
        with mon._stdin_access_lock:
            mon._restore_stdin()
            try:
                selection = input().strip()
            finally:
                mon._set_stdin_raw()

        regions_to_erase = []

        if selection.lower() == 'all':
            regions_to_erase = partitions
        elif selection:
            try:
                indices = [int(x.strip()) for x in selection.split(',')]
                for idx in indices:
                    if 1 <= idx <= len(partitions):
                        regions_to_erase.append(partitions[idx - 1])
                    else:
                        nprint(f"[erase] Índice inválido: {idx}")
            except ValueError:
                nprint("[erase] Entrada inválida, usando modo manual...")
                regions_to_erase = []
        else:
            regions_to_erase = []

        if not regions_to_erase:
            # Modo manual
            nprint("\r\n[erase] Modo manual - Ingresa offset y tamaño (hex, ej: 0x9000 0x6000):")
            nprint("[erase] O 'cancel' para cancelar: ")
            with mon._stdin_access_lock:
                mon._restore_stdin()
                try:
                    manual_input = input().strip()
                finally:
                    mon._set_stdin_raw()

            if manual_input.lower() == 'cancel':
                nprint("[erase] Operación cancelada")
                return

            try:
                parts = manual_input.split()
                if len(parts) >= 2:
                    offset = int(parts[0], 16)
                    size = int(parts[1], 16)
                    regions_to_erase = [{'offset': offset, 'size': size, 'name': 'manual'}]
                else:
                    nprint("[erase] Formato inválido")
                    return
            except ValueError as e:
                nprint(f"[erase] Error parseando valores: {e}")
                return
    else:
        # No se detectó tabla, modo manual
        nprint("\r\n[erase] No se detectó tabla de particiones")
        nprint("[erase] Ingresa offset y tamaño en hex (ej: 0x9000 0x6000):")
        nprint("[erase] O 'cancel' para cancelar: ")
        with mon._stdin_access_lock:
            mon._restore_stdin()
            try:
                manual_input = input().strip()
            finally:
                mon._set_stdin_raw()

        if manual_input.lower() == 'cancel':
            nprint("[erase] Operación cancelada")
            return

        try:
            parts = manual_input.split()
            if len(parts) >= 2:
                offset = int(parts[0], 16)
                size = int(parts[1], 16)
                regions_to_erase = [{'offset': offset, 'size': size, 'name': 'manual'}]
            else:
                nprint("[erase] Formato inválido. Usa: offset_hex size_hex")
                return
        except ValueError as e:
            nprint(f"[erase] Error parseando valores: {e}")
            return

    # Confirmar antes de borrar
    nprint(f"\r\n[erase] Se borrarán {len(regions_to_erase)} región(es):")
    for r in regions_to_erase:
        offset_hex = f"0x{r['offset']:x}" if isinstance(r['offset'], int) else r.get('offset_hex', 'N/A')
        size_hex = f"0x{r['size']:x}" if isinstance(r['size'], int) else r.get('size_hex', 'N/A')
        name = r.get('name', 'manual')
        nprint(f"  - {name}: offset {offset_hex}, tamaño {size_hex}")

    nprint("\r\n[erase] ¿Confirmar? (s/N): ")
    with mon._stdin_access_lock:
        mon._restore_stdin()
        try:
            confirm = input().strip().lower()
        finally:
            mon._set_stdin_raw()

    if confirm != 's':
        nprint("[erase] Operación cancelada")
        return

    # Ejecutar erase_region para cada región
    try:
        esptool = find_esptool_cmd()
        print(esptool)
        chip = cfg.get("chip", "auto")
        tty = cfg.get("tty")
        flash_baud = cfg.get("flash_baud", 921600)

        # Establecer bandera para ignorar señales durante operación temporal
        _ignore_signals_flag.set()
        try:
            nprint("\r\n[erase] Deteniendo monitor temporalmente...")
            mon.stop()

            for r in regions_to_erase:
                offset = r['offset'] if isinstance(r['offset'], int) else int(r['offset_hex'], 16)
                size = r['size'] if isinstance(r['size'], int) else int(r['size_hex'], 16)
                name = r.get('name', 'manual')

                offset_hex = f"0x{offset:x}"
                size_hex = f"0x{size:x}"

                nprint(f"\r\n[erase] Borrando {name} @ {offset_hex} (tamaño {size_hex})...")

                cmd = esptool + [
                    # "--chip", chip,
                    "--port", tty,
                    "--after", "no-reset",
                    # "--baud", str(flash_baud),
                    "erase_region", offset_hex, size_hex,
                    "--force"

                ]

                svc_log.info(f"[erase] Ejecutando: {' '.join(shlex.quote(c) for c in cmd)}\r\n")
                rc = run_cmd(cmd, svc_log)

                if rc == 0:
                    nprint(f"[erase] Región {name} borrada exitosamente")
                else:
                    nprint(f"[erase] Error borrando región {name} (código {rc})")

            nprint("\r\n[erase] Reiniciando monitor...")
            mon.start()
            nprint("[erase] Operación completada")
        finally:
            # Limpiar bandera para volver a escuchar señales
            _ignore_signals_flag.clear()

    except Exception as e:
        nprint(f"\r\n[erase] ERROR: {e}")
        svc_log.exception(f"[erase] Error en erase_region: {e}\r\n")
        # Asegurar que la bandera se limpie incluso en caso de error
        _ignore_signals_flag.clear()
        try:
            mon.start()
        except Exception:
            pass


# ========== Monitor serial ==========

class EspMonitor:
    """
    Monitor serial basado en esp_idf_monitor.
    Interfaz pública identica a PicocomMonitor.
    """

    def __init__(self, tty_path: str, baud: int, logs_dir: pathlib.Path,
                 elf_path=None, cfg: dict = None, svc_log: logging.Logger = None):
        self.tty_path = tty_path
        self.baud = baud
        self.logs_dir = logs_dir
        self.elf_path = elf_path
        self.cfg = cfg or {}
        self.svc_log = svc_log
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.logfile: pathlib.Path | None = None
        self.master_fd: int | None = None
        self._stdin_fd: int | None = None
        self._stdin_old_attrs = None
        # Buffer circular para salida reciente (últimos 64KB)
        self._output_buffer = bytearray()
        self._output_buffer_max = 64 * 1024  # 64 KB
        self._output_buffer_lock = threading.Lock()
        # Lock para proteger acceso a stdin durante erase_region
        self._stdin_access_lock = threading.Lock()

    def _set_stdin_raw(self):
        # solo si corremos en una TTY (tmux/ssh interactivo)
        if sys.stdin.isatty():
            self._stdin_fd = sys.stdin.fileno()
            self._stdin_old_attrs = termios.tcgetattr(self._stdin_fd)
            tty.setraw(self._stdin_fd, when=termios.TCSANOW)

    def _restore_stdin(self):
        if self._stdin_fd is not None and self._stdin_old_attrs is not None:
            try:
                termios.tcsetattr(self._stdin_fd, termios.TCSANOW, self._stdin_old_attrs)
            except Exception:
                pass
        self._stdin_fd = None
        self._stdin_old_attrs = None

    def get_recent_output(self) -> str:
        """Obtiene la salida reciente del monitor como string"""
        with self._output_buffer_lock:
            return self._output_buffer.decode('utf-8', errors='replace')

    def start(self):
        if self.proc and self.proc.poll() is None:
            nprint("[monitor] ya está corriendo, ignorando start()")
            return  # ya corriendo
        nprint(f"[monitor] iniciando esp_idf_monitor en {self.tty_path} @ {self.baud} baud")
        self.stop_flag.clear()
        daydir = self.logs_dir / time.strftime("%Y%m%d")
        daydir.mkdir(parents=True, exist_ok=True)
        self.logfile = daydir / "serial.log"
        nprint(f"[monitor] archivo de log: {self.logfile}")

        cmd = [sys.executable, "-m", "esp_idf_monitor", "--port", self.tty_path, "--baud", str(self.baud)]
        if self.elf_path and pathlib.Path(self.elf_path).exists():
            cmd += ["--elf", str(self.elf_path)]
        nprint(f"[monitor] comando: {' '.join(cmd)}")

        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        nprint(f"[monitor] PTY creado, master_fd={master_fd}, slave_fd={slave_fd}")
        self.proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        nprint(f"[monitor] esp_idf_monitor iniciado (PID={self.proc.pid})")

        # Terminal del usuario en modo raw para pasar teclas especiales
        self._set_stdin_raw()
        if self._stdin_fd is not None:
            nprint(f"[monitor] stdin configurado en modo raw")

        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()
        nprint("[monitor] hilo de pump iniciado")

    def _pump(self):
        assert self.master_fd is not None
        with open(self.logfile, "ab", buffering=0) as lf:
            while not self.stop_flag.is_set():
                rlist = [self.master_fd]
                if self._stdin_fd is not None:
                    rlist.append(self._stdin_fd)

                r, _, _ = select.select(rlist, [], [], SELECT_TIMEOUT)

                # --- salida desde esp_idf_monitor ---
                if self.master_fd in r:
                    try:
                        chunk = os.read(self.master_fd, PTY_READ_SIZE)
                    except OSError:
                        break
                    if chunk:
                        # Guardar en buffer para análisis de tabla de particiones
                        with self._output_buffer_lock:
                            self._output_buffer.extend(chunk)
                            # Mantener solo los últimos N bytes
                            if len(self._output_buffer) > self._output_buffer_max:
                                self._output_buffer = self._output_buffer[-self._output_buffer_max:]

                        try:
                            # Normalizar saltos de línea para evitar logs corridos
                            normalized_chunk = normalize_line_endings(chunk)
                            sys.stdout.buffer.write(normalized_chunk)
                            sys.stdout.buffer.flush()
                        except Exception:
                            pass
                        try:
                            # Para el archivo de log, mantener saltos de línea originales
                            lf.write(chunk)
                        except Exception:
                            pass
                    else:
                        if self.proc and self.proc.poll() is not None:
                            break

                # --- entrada desde teclado ---
                if self._stdin_fd is not None and self._stdin_fd in r:
                    try:
                        data = os.read(self._stdin_fd, STDIN_READ_SIZE)
                    except OSError:
                        data = b""
                    if not data:
                        continue

                    # Detectar Ctrl-C (byte 0x03)
                    if b"\x03" in data:
                        sys.stdout.buffer.write(b"\r\n")
                        sys.stdout.buffer.flush()
                        nprint("[monitor] Ctrl-C detectado -> terminando servidor...")
                        self.stop_flag.set()
                        os.kill(os.getpid(), signal.SIGINT)
                        return

                    # Detectar Ctrl-E (byte 0x05) para modo erase region
                    if b"\x05" in data:
                        sys.stdout.buffer.write(b"\r\n")
                        sys.stdout.buffer.flush()
                        # Llamar a erase_region_interactive en un hilo separado
                        # para no bloquear el pump
                        def run_erase():
                            try:
                                erase_region_interactive(self, self.cfg, self.svc_log)
                            except Exception as e:
                                nprint(f"[erase] ERROR: {e}")
                                if self.svc_log:
                                    self.svc_log.exception(f"[erase] Error: {e}\r\n")

                        erase_thread = threading.Thread(target=run_erase, daemon=True)
                        erase_thread.start()
                        continue  # No reenviar Ctrl-E al monitor

                    # Si no es una combinación especial, reenviamos al monitor
                    if self.master_fd is not None:
                        try:
                            os.write(self.master_fd, data)
                        except OSError:
                            pass

    def stop(self):
        nprint(f"\r\n[monitor] stop() llamado - PID del proceso: {os.getpid()}")
        nprint(f"[monitor] self.proc: {self.proc}")
        nprint(f"[monitor] self.proc.poll(): {self.proc.poll() if self.proc else 'None'}")

        if not self.proc or self.proc.poll() is not None:
            nprint("[monitor] proceso ya terminado o no existe")
            self._restore_stdin()
            return

        nprint("[monitor] stopping esp_idf_monitor...")
        self.stop_flag.set()

        try:
            nprint(f"[monitor] enviando SIGTERM a PID {self.proc.pid}")
            self.proc.terminate()
            nprint("[monitor] SIGTERM enviado, esperando...")

            try:
                self.proc.wait(timeout=TIMEOUT_PROCESS_TERMINATE)
                nprint("[monitor] esp_idf_monitor terminado suavemente")
            except subprocess.TimeoutExpired:
                nprint("[monitor] timeout, forzando terminación...")
                try:
                    self.proc.kill()
                    nprint(f"[monitor] SIGKILL enviado a PID {self.proc.pid}")
                    try:
                        self.proc.wait(timeout=1)
                        nprint("[monitor] esp_idf_monitor terminado por fuerza")
                    except subprocess.TimeoutExpired:
                        nprint("[monitor] WARNING: esp_idf_monitor no terminó completamente")
                except Exception as kill_e:
                    nprint(f"[monitor] ERROR al enviar SIGKILL: {kill_e}")
        except Exception as e:
            nprint(f"[monitor] ERROR al terminar esp_idf_monitor: {e}")

        nprint("[monitor] cerrando hilo...")
        if self.thread:
            try:
                self.thread.join(timeout=TIMEOUT_THREAD_JOIN)
                nprint("[monitor] hilo cerrado")
            except Exception as e:
                nprint(f"[monitor] ERROR cerrando hilo: {e}")

        nprint("[monitor] cerrando file descriptors...")
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
                nprint(f"[monitor] master_fd {self.master_fd} cerrado")
            except Exception as e:
                nprint(f"[monitor] ERROR cerrando master_fd: {e}")
            self.master_fd = None

        nprint("[monitor] restaurando stdin...")
        self._restore_stdin()
        nprint("[monitor] esp_idf_monitor stopped - método completado")

    def restart(self):
        self.stop()
        time.sleep(MONITOR_RESTART_DELAY)
        # Re-check elf_path exists at restart time (may have been updated)
        self.start()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor.py — Monitor serial basado en esp_idf_monitor para ESP32

EspMonitor reemplaza a PicocomMonitor lanzando esp_idf_monitor en lugar de picocom,
con soporte de ELF para decodificación de backtraces.
"""

import logging, os, pathlib, select, shlex, signal, subprocess, sys, threading, time, pty, tty, termios

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from server.flash import find_esptool_cmd, run_cmd

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
    sys.stdout.write(s + "\r\n")
    sys.stdout.flush()


def normalize_line_endings(data: bytes) -> bytes:
    result = data.replace(b'\r\n', b'\n')
    result = result.replace(b'\n', b'\r\n')
    return result


# ========== Monitor serial ==========

class EspMonitor:
    """
    Monitor serial basado en esp_idf_monitor.
    Interfaz pública identica a PicocomMonitor.
    """

    def __init__(self, tty_path: str, baud: int, logs_dir: pathlib.Path,
                 elf_path=None, cfg: dict = None, svc_log: logging.Logger = None,
                 on_ctrl_e=None):
        self.tty_path = tty_path
        # Ctrl-E (modo Erase Region) se inyecta desde afuera: el monitor no
        # sabe nada de esptool ni de la FSM del device.
        self._on_ctrl_e = on_ctrl_e
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

    def interactive_input(self, prompt: str) -> str:
        """Pide una línea al operador en la terminal de la sesión tmux.

        Saca stdin de modo raw mientras dura el input() y lo vuelve a poner
        después. Es la única forma en que código de afuera debe leer teclado:
        nada fuera de esta clase toca _stdin_access_lock / _restore_stdin."""
        with self._stdin_access_lock:
            self._restore_stdin()
            try:
                sys.stdout.write(prompt)
                sys.stdout.flush()
                return input().strip()
            except EOFError:
                return ""
            finally:
                self._set_stdin_raw()

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
            cmd += [str(self.elf_path)]
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
                        # Ctrl-E corre en un hilo aparte para no bloquear el pump
                        if self._on_ctrl_e is None:
                            nprint("[monitor] Ctrl-E: modo erase no disponible")
                            continue
                        threading.Thread(target=self._run_ctrl_e, daemon=True).start()
                        continue  # No reenviar Ctrl-E al monitor

                    # Si no es una combinación especial, reenviamos al monitor
                    if self.master_fd is not None:
                        try:
                            os.write(self.master_fd, data)
                        except OSError:
                            pass

    def _run_ctrl_e(self):
        try:
            self._on_ctrl_e()
        except Exception as e:
            nprint(f"[erase] ERROR: {e}")
            if self.svc_log:
                self.svc_log.exception(f"[erase] Error: {e}\r\n")

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

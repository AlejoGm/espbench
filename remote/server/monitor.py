#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor.py — EspMonitor: esp_idf_monitor corriendo en un PTY.

Relaya la salida del monitor a la terminal (la sesión tmux, para
`devremote <N>`) y a un sink (el DeviceLog, que es lo que ve el dashboard),
y el teclado de la sesión al monitor. Guarda los últimos 64 KB en un buffer
circular para quien necesite mirar lo que imprimió el firmware (tabla de
particiones para el erase, MAC por serial).

Teclas propias: Ctrl-C termina el proceso; Ctrl-E llama a on_ctrl_e (modo
Erase Region, inyectado desde afuera — el monitor no sabe de esptool ni de
la FSM del device). El resto va al monitor, pasando antes por input_sink
(DeviceManager.on_keys: un Ctrl-T Ctrl-R es un reset a propósito).
"""
import os
import pathlib
import pty
import select
import signal
import subprocess
import sys
import termios
import threading
import tty
from typing import Callable, Optional, Union

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from server import taglog

TAG = "monitor"

PTY_READ_SIZE = 4096
STDIN_READ_SIZE = 1024
SELECT_TIMEOUT = 0.1
TIMEOUT_PROCESS_TERMINATE = 3
TIMEOUT_THREAD_JOIN = 2
OUTPUT_BUFFER_MAX = 64 * 1024

ElfPath = Union[None, str, pathlib.Path, Callable[[], Optional[pathlib.Path]]]


def normalize_line_endings(data: bytes) -> bytes:
    """\\n sueltos → \\r\\n, para que la terminal en modo raw no escalone."""
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


class EspMonitor:

    def __init__(self, tty_path: str, baud: int,
                 output_sink: Optional[Callable[[bytes], None]] = None,
                 elf_path: ElfPath = None,
                 on_ctrl_e: Optional[Callable[[], None]] = None,
                 input_sink: Optional[Callable[[bytes], None]] = None):
        """elf_path puede ser un path o una función que lo devuelva: se resuelve
        en cada start(), porque el .elf cambia con cada flash y su ubicación
        depende de si ya se conoce la MAC del device."""
        self.tty_path = tty_path
        self.baud = baud
        self._output_sink = output_sink
        self._elf_path = elf_path
        self._on_ctrl_e = on_ctrl_e
        self._input_sink = input_sink
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.stop_flag = threading.Event()
        self.master_fd: Optional[int] = None
        self._stdin_fd: Optional[int] = None
        self._stdin_old_attrs = None
        self._output_buffer = bytearray()
        self._output_buffer_lock = threading.Lock()
        self._stdin_access_lock = threading.Lock()

    # ---------- interfaz pública ----------

    def get_recent_output(self) -> str:
        with self._output_buffer_lock:
            return self._output_buffer.decode("utf-8", errors="replace")

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

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            taglog.debug(TAG, "ya está corriendo, ignorando start()")
            return
        self.stop_flag.clear()
        cmd = [sys.executable, "-m", "esp_idf_monitor", "--port", self.tty_path, "--baud", str(self.baud)]
        elf = self._resolve_elf()
        if elf:
            cmd.append(str(elf))
        taglog.info(TAG, f"iniciando esp_idf_monitor en {self.tty_path} @ {self.baud}"
                         + (f" (elf: {elf})" if elf else " (sin elf: backtraces sin decodificar)"))

        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        self.proc = subprocess.Popen(cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True)
        os.close(slave_fd)
        taglog.debug(TAG, f"esp_idf_monitor PID={self.proc.pid}")

        self._set_stdin_raw()
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            self._restore_stdin()
            return
        taglog.info(TAG, "deteniendo esp_idf_monitor")
        self.stop_flag.set()
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=TIMEOUT_PROCESS_TERMINATE)
            except subprocess.TimeoutExpired:
                taglog.warn(TAG, "esp_idf_monitor no terminó, forzando (SIGKILL)")
                self.proc.kill()
                try:
                    self.proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    taglog.error(TAG, "esp_idf_monitor no terminó ni con SIGKILL")
        except Exception as e:
            taglog.error(TAG, f"error al terminar esp_idf_monitor: {e}")
        if self.thread:
            self.thread.join(timeout=TIMEOUT_THREAD_JOIN)
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        self._restore_stdin()

    # ---------- internos ----------

    def _resolve_elf(self) -> Optional[pathlib.Path]:
        elf = self._elf_path() if callable(self._elf_path) else self._elf_path
        if elf and pathlib.Path(elf).exists():
            return pathlib.Path(elf)
        return None

    def _set_stdin_raw(self) -> None:
        # solo si corremos en una TTY (sesión tmux / ssh interactivo)
        if sys.stdin.isatty():
            self._stdin_fd = sys.stdin.fileno()
            self._stdin_old_attrs = termios.tcgetattr(self._stdin_fd)
            tty.setraw(self._stdin_fd, when=termios.TCSANOW)

    def _restore_stdin(self) -> None:
        if self._stdin_fd is not None and self._stdin_old_attrs is not None:
            try:
                termios.tcsetattr(self._stdin_fd, termios.TCSANOW, self._stdin_old_attrs)
            except Exception:
                pass
        self._stdin_fd = None
        self._stdin_old_attrs = None

    def _on_output(self, chunk: bytes) -> None:
        with self._output_buffer_lock:
            self._output_buffer.extend(chunk)
            if len(self._output_buffer) > OUTPUT_BUFFER_MAX:
                self._output_buffer = self._output_buffer[-OUTPUT_BUFFER_MAX:]
        try:
            sys.stdout.buffer.write(normalize_line_endings(chunk))
            sys.stdout.buffer.flush()
        except Exception:
            pass
        if self._output_sink:
            try:
                self._output_sink(chunk)
            except Exception:
                pass

    def _pump(self) -> None:
        assert self.master_fd is not None
        while not self.stop_flag.is_set():
            rlist = [self.master_fd]
            if self._stdin_fd is not None:
                rlist.append(self._stdin_fd)
            r, _, _ = select.select(rlist, [], [], SELECT_TIMEOUT)

            if self.master_fd in r:
                try:
                    chunk = os.read(self.master_fd, PTY_READ_SIZE)
                except OSError:
                    break
                if chunk:
                    self._on_output(chunk)
                elif self.proc and self.proc.poll() is not None:
                    break

            if self._stdin_fd is not None and self._stdin_fd in r:
                try:
                    data = os.read(self._stdin_fd, STDIN_READ_SIZE)
                except OSError:
                    data = b""
                if not data:
                    continue
                if b"\x03" in data:    # Ctrl-C: terminar el proceso
                    sys.stdout.buffer.write(b"\r\n")
                    sys.stdout.buffer.flush()
                    taglog.info(TAG, "Ctrl-C: terminando")
                    self.stop_flag.set()
                    os.kill(os.getpid(), signal.SIGINT)
                    return
                if b"\x05" in data:    # Ctrl-E: modo Erase Region, en otro hilo
                    sys.stdout.buffer.write(b"\r\n")
                    sys.stdout.buffer.flush()
                    if self._on_ctrl_e is None:
                        taglog.warn(TAG, "Ctrl-E: modo erase no disponible")
                    else:
                        threading.Thread(target=self._run_ctrl_e, daemon=True).start()
                    continue
                if self._input_sink is not None:
                    try:
                        self._input_sink(data)
                    except Exception as e:
                        taglog.debug(TAG, f"input_sink: {e}")
                if self.master_fd is not None:
                    try:
                        os.write(self.master_fd, data)
                    except OSError:
                        pass

    def _run_ctrl_e(self) -> None:
        try:
            self._on_ctrl_e()
        except Exception as e:
            taglog.error(TAG, f"Ctrl-E: {e}")

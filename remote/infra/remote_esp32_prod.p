#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
remote_esp32.py — Monitor persistente con picocom + flasheo remoto

✅ Usa picocom como monitor serial (logs en stdout + archivo)
✅ Escucha un puerto TCP para recibir órdenes de flash
✅ Mata picocom, flashea con esptool, y lo relanza automáticamente
"""

import argparse, datetime as dt, json, logging, os, pathlib, shlex, shutil, signal, socket, struct, subprocess, sys, tempfile, threading, time, zipfile, hashlib
import pty, select
import tty, termios, pathlib
from urllib.request import urlopen, Request

# Bandera global para terminación
_shutdown_flag = threading.Event()
# Bandera para ignorar señales durante operaciones temporales (erase_region, etc)
_ignore_signals_flag = threading.Event()

# Handler de señales para debugging y terminación
def signal_handler(signum, frame):
    # Ignorar señales durante operaciones temporales (erase_region, etc)
    if _ignore_signals_flag.is_set():
        print(f"\r\n[DEBUG] Señal {signum} ignorada (operación temporal en curso)\r\n", flush=True)
        return
    
    print(f"\r\n[DEBUG] Señal recibida: {signum}\r\n", flush=True)
    print(f"[DEBUG] PID del proceso: {os.getpid()}\r\n", flush=True)
    # print(f"[DEBUG] Stack trace:\r\n", flush=True)
    # import traceback
    # traceback.print_stack(frame)
    # print(f"[DEBUG] Fin del stack trace\r\n", flush=True)
    # Establecer bandera de terminación
    _shutdown_flag.set()

# Registrar handlers para señales comunes
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGHUP, signal_handler)

# ========== Constantes ==========
CHUNK_SIZE = 1024 * 1024  # 1 MB
CHUNK_PROGRESS_INTERVAL = 5 * CHUNK_SIZE  # Log cada 5 MB
TCP_BACKLOG = 5
TIMEOUT_DOWNLOAD = 120  # segundos
TIMEOUT_PROCESS_TERMINATE = 3  # segundos
TIMEOUT_THREAD_JOIN = 2  # segundos
MONITOR_RESTART_DELAY = 0.8  # segundos
PTY_READ_SIZE = 4096  # bytes
STDIN_READ_SIZE = 1024  # bytes
SELECT_TIMEOUT = 0.1  # segundos
SHA256_READ_CHUNK = 1024 * 1024  # 1 MB para hash
DEFAULT_FLASH_FILES = [
    ("0x1000", "bootloader.bin"),
    ("0x8000", "partition-table.bin"),
    ("0xe000", "ota_data_initial.bin"),
    ("0x10000", "app.bin")
]

def nprint(s):
    print(s + "\r\n", flush=True)

def normalize_line_endings(data: bytes) -> bytes:
    """
    Normaliza los saltos de línea para terminal:
    - Mantiene \r\n existentes
    - Convierte \n solos a \r\n
    - Convierte \r solos a \r\n (para consistencia)
    """
    # Primero, normalizar \r\n existentes (asegurar que no se dupliquen)
    # Reemplazar cualquier secuencia de \r seguida de \n con \r\n único
    result = data.replace(b'\r\n', b'\n')  # Temporalmente convertir a \n
    # result = result.replace(b'\r', b'\n')  # Convertir \r solos a \n
    result = result.replace(b'\n', b'\r\n')  # Convertir todos los \n a \r\n
    return result

# ========== Utilidades generales ==========

def ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)

def setup_logging(logs_dir: pathlib.Path):
    ensure_dir(logs_dir)
    service_log = logs_dir / "remote_esp32.service.log"
    L = logging.getLogger("svc")
    L.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout); sh.setFormatter(fmt); L.addHandler(sh)
    fh = logging.FileHandler(service_log, encoding="utf-8"); fh.setFormatter(fmt); L.addHandler(fh)
    return L

def day_log_path(base: pathlib.Path):
    daydir = base / dt.datetime.now().strftime("%Y%m%d")
    ensure_dir(daydir)
    return daydir / "serial.log"

def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(SHA256_READ_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()

def find_esptool_cmd():
    # Buscar esptool.py en el PATH
    exe = shutil.which("esptool.py")
    if exe: return [exe]
    
    # Buscar en el entorno virtual del usuario
    venv_esptool = "/home/sfypi/espvenv/bin/esptool.py"
    if os.path.exists(venv_esptool):
        return [venv_esptool]
    
    # Buscar esptool como módulo Python
    try:
        import esptool
        return [sys.executable, "-m", "esptool"]
    except ImportError:
        pass
    
    # Intentar con el entorno virtual
    venv_python = "/home/sfypi/espvenv/bin/python"
    if os.path.exists(venv_python):
        return [venv_python, "-m", "esptool"]
    
    raise RuntimeError("esptool no encontrado. Instala con: pip install esptool")

# === Funciones para erase region ==========

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

def erase_region_interactive(mon: 'PicocomMonitor', cfg: dict, svc_log: logging.Logger):
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
                    nprint(f"[erase] ✓ Región {name} borrada exitosamente")
                else:
                    nprint(f"[erase] ✗ Error borrando región {name} (código {rc})")
            
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
        except:
            pass

# === Monitor serial ==========

class PicocomMonitor:
    def __init__(self, tty_path: str, baud: int, logs_dir: pathlib.Path, cfg: dict = None, svc_log: logging.Logger = None):
        self.tty_path = tty_path
        self.baud = baud
        self.logs_dir = logs_dir
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
        nprint(f"[monitor] iniciando monitor en {self.tty_path} @ {self.baud} baud")
        self.stop_flag.clear()
        daydir = self.logs_dir / time.strftime("%Y%m%d")
        daydir.mkdir(parents=True, exist_ok=True)
        self.logfile = daydir / "serial.log"
        nprint(f"[monitor] archivo de log: {self.logfile}")

        cmd = ["picocom", "-b", str(self.baud), self.tty_path]
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
        nprint(f"[monitor] picocom iniciado (PID={self.proc.pid})")

        # Terminal del usuario en modo raw para pasar Ctrl-A, etc.
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

                # --- salida desde picocom ---
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
                        nprint("[monitor] Ctrl-C detectado → terminando servidor...")
                        self.stop_flag.set()
                        # Esto rompe el loop principal del script
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
                        continue  # No reenviar Ctrl-E a picocom

                    # Si no es una combinación especial, reenviamos a picocom
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
        
        nprint("[monitor] stopping picocom...")
        self.stop_flag.set()
        
        try:
            nprint(f"[monitor] enviando SIGTERM a PID {self.proc.pid}")
            # Intentar terminar suavemente
            self.proc.terminate()
            nprint("[monitor] SIGTERM enviado, esperando...")
            
            try:
                self.proc.wait(timeout=TIMEOUT_PROCESS_TERMINATE)
                nprint("[monitor] picocom terminado suavemente")
            except subprocess.TimeoutExpired:
                nprint("[monitor] timeout, forzando terminación...")
                try:
                    self.proc.kill()
                    nprint(f"[monitor] SIGKILL enviado a PID {self.proc.pid}")
                    try:
                        self.proc.wait(timeout=1)
                        nprint("[monitor] picocom terminado por fuerza")
                    except subprocess.TimeoutExpired:
                        nprint("[monitor] WARNING: picocom no terminó completamente")
                except Exception as kill_e:
                    nprint(f"[monitor] ERROR al enviar SIGKILL: {kill_e}")
        except Exception as e:
            nprint(f"[monitor] ERROR al terminar picocom: {e}")
        
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
        nprint("[monitor] picocom stopped - método completado")

    def restart(self):
        self.stop()
        time.sleep(MONITOR_RESTART_DELAY)
        self.start()

# === Flasheo ==========

def build_esptool_cmd(esptool_cmd, chip, port, baud, encrypt, erase, jobdir: pathlib.Path):
    fa = jobdir / "flasher_args.json"
    nprint(f"[build_cmd] leyendo {fa}")
    J = json.loads(fa.read_text(encoding="utf-8"))
    nprint(f"[build_cmd] contenido: {json.dumps(J, indent=2)}")

    pairs = []
    ff = J.get("flash_files")
    
    # Intentar como diccionario primero
    if isinstance(ff, dict):
        nprint(f"[build_cmd] flash_files es diccionario con {len(ff)} entradas")
        for off, path in ff.items():
            if off and path:
                p = pathlib.Path(path)
                # Buscar archivo con diferentes estrategias
                candidates = [
                    p if p.is_absolute() else jobdir / p,
                    jobdir / p.name,
                    jobdir / p.parts[-1] if p.parts else jobdir / str(p)
                ]
                found = False
                for cand in candidates:
                    if cand.exists():
                        nprint(f"[build_cmd] encontrado: {off} → {cand}")
                        pairs.append((off, str(cand)))
                        found = True
                        break
                if not found:
                    nprint(f"[build_cmd] NO ENCONTRADO: {off} → {path}")
    
    # Intentar como lista
    elif isinstance(ff, list):
        nprint(f"[build_cmd] encontrados {len(ff)} entradas en flash_files")
        for it in ff:
            off, path = None, None
            if isinstance(it, (list, tuple)) and len(it) >= 2:
                off, path = it[0], it[1]
            elif isinstance(it, dict):
                off = it.get("offset")
                path = it.get("file") or it.get("bin_file") or it.get("path")
            if off and path:
                p = pathlib.Path(path)
                cand = p if p.is_absolute() else (jobdir / p.name if (jobdir / p.name).exists() else jobdir / path)
                if cand.exists():
                    nprint(f"[build_cmd] agregando par: {off} → {cand}")
                    pairs.append((off, str(cand)))
                else:
                    nprint(f"[build_cmd] NO ENCONTRADO: {off} → {cand}")
    
    # Si no se encontraron pares, buscar archivos por defecto
    if not pairs:
        nprint(f"[build_cmd] no se encontraron archivos en flash_files, buscando archivos por defecto...")
        for off, name in DEFAULT_FLASH_FILES:
            p = jobdir / name
            if p.exists():
                nprint(f"[build_cmd] encontrado: {off} → {p}")
                pairs.append((off, str(p)))
            else:
                nprint(f"[build_cmd] no encontrado: {p}")
    
    # Si aún no hay pares, buscar cualquier .bin en el directorio
    if not pairs:
        nprint(f"[build_cmd] buscando cualquier archivo .bin en {jobdir}...")
        for bin_file in jobdir.glob("*.bin"):
            nprint(f"[build_cmd] archivo .bin encontrado: {bin_file}")
            # Intentar asignar offsets comunes basados en el nombre
            name = bin_file.name.lower()
            if "bootloader" in name:
                pairs.append(("0x1000", str(bin_file)))
                nprint(f"[build_cmd] asignado bootloader: 0x1000 → {bin_file}")
            elif "partition" in name:
                pairs.append(("0x8000", str(bin_file)))
                nprint(f"[build_cmd] asignado partition: 0x8000 → {bin_file}")
            elif "ota" in name:
                pairs.append(("0xe000", str(bin_file)))
                nprint(f"[build_cmd] asignado ota: 0xe000 → {bin_file}")
            elif "app" in name or "main" in name or "firmware" in name or bin_file.name.endswith(".bin"):
                # Asignar a offset común de aplicación
                pairs.append(("0x10000", str(bin_file)))
                nprint(f"[build_cmd] asignado app: 0x10000 → {bin_file}")
    
    nprint(f"[build_cmd] total de pares encontrados: {len(pairs)}")
    if len(pairs) == 0:
        raise FileNotFoundError("No se encontraron archivos .bin para flashear")
    
    # Verificar si tenemos archivos críticos
    offsets = [off for off, _ in pairs]
    critical_files = []
    
    # Verificar archivos críticos basados en offsets comunes
    if "0x1000" not in offsets:
        critical_files.append("bootloader (0x1000)")
    if "0x10000" not in offsets and "0x120000" not in offsets:
        critical_files.append("aplicación (0x10000 o 0x120000)")
    
    if critical_files:
        nprint(f"[build_cmd] WARNING: Faltan archivos críticos: {', '.join(critical_files)}")
        nprint("[build_cmd] El ESP32 puede no funcionar correctamente sin estos archivos")
        
        # Si falta la aplicación, es crítico
        if "aplicación" in str(critical_files):
            nprint("[build_cmd] ERROR: Falta el archivo de aplicación principal")
            nprint("[build_cmd] Sin aplicación, el ESP32 no funcionará correctamente")
            nprint("[build_cmd] Continuando con flasheo parcial...")
            nprint("[build_cmd] NOTA: El ESP32 necesitará ser flasheado con aplicación después")
            
            # En lugar de fallar, continuar con advertencia
            # raise FileNotFoundError(f"Archivo de aplicación faltante. Archivos críticos faltantes: {', '.join(critical_files)}")
    
    nprint(f"[build_cmd] Archivos listos para flashear: {len(pairs)}")
    for off, path in pairs:
        nprint(f"  ✓ {off}: {pathlib.Path(path).name}")

    erase_cmd = None
    if erase:
        erase_cmd = esptool_cmd + ["--chip", chip, "--port", port, "--baud", str(baud), "erase-flash"]

    write_cmd = esptool_cmd + ["--chip", chip, "--port", port, "--baud", str(baud),
                               "--before", "default-reset", "--after", "hard-reset",
                               "write-flash", "-z"]
    if encrypt:
        # Solo agregar --encrypt si no está ya encriptado
        # Para ESP32 con flash encryption ya habilitado, no usar --encrypt
        write_cmd.insert(write_cmd.index("write-flash") + 1, "--encrypt")
    for off, p in pairs:
        write_cmd += [off, p]
    return erase_cmd, write_cmd, pairs

def run_cmd(cmd, log: logging.Logger):
    log.info("RUN: %s", " ".join(shlex.quote(c) for c in cmd))
    nprint(f">>> {' '.join(shlex.quote(c) for c in cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        log.info(line.rstrip())
        print(line.rstrip(), flush=True)
    rc = proc.wait()
    log.info("EXIT %d", rc)
    nprint(f"<<< EXIT CODE: {rc}")
    return rc

# ========== Control TCP ==========

def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf

def handle_control(sock, cfg, mon: PicocomMonitor, svc_log: logging.Logger):
    nprint(f"[control] handle_control iniciado - PID: {os.getpid()}")
    svc_log.info(f"[control] handle_control iniciado - PID: {os.getpid()}\r\n")
    
    # 1) header
    svc_log.info("[control] recibiendo header...\r\n")
    hdr_len = struct.unpack(">I", recv_exact(sock, 4))[0]
    svc_log.info(f"[control] tamaño del header: {hdr_len} bytes\r\n")
    header = json.loads(recv_exact(sock, hdr_len).decode("utf-8"))
    svc_log.info(f"[control] header recibido: {json.dumps(header, indent=2)}\r\n")

    token = str(cfg["token"] or "")
    if token and header.get("token") != token:
        svc_log.warning("[control] token inválido, rechazando conexión\r\n")
        resp = {"ok": False, "error": "unauthorized"}
        payload = json.dumps(resp).encode(); sock.sendall(struct.pack(">I", len(payload)) + payload)
        return

    action = header.get("action")
    svc_log.info(f"[control] acción solicitada: {action}\r\n")
    if action not in ("upload_and_flash", "pull_and_flash"):
        svc_log.error(f"[control] acción inválida: {action}")
        resp = {"ok": False, "error": "bad_action"}
        payload = json.dumps(resp).encode(); sock.sendall(struct.pack(">I", len(payload)) + payload)
        return

    # 2) preparar job + ACK
    job_id = header.get("job_id") or time.strftime("job_%Y%m%d_%H%M%S")
    svc_log.info(f"[control] job_id: {job_id}\r\n")
    jobs_dir: pathlib.Path = cfg["jobs_dir"]
    logs_dir: pathlib.Path = cfg["logs_dir"]
    jobdir = jobs_dir / job_id; ensure_dir(jobdir)
    svc_log.info(f"[control] directorio de trabajo: {jobdir}\r\n")
    artifact = jobdir / "artifact.zip"

    ack = {"ok": True, "phase": "ready", "job_id": job_id}
    payload = json.dumps(ack).encode()
    sock.sendall(struct.pack(">I", len(payload)) + payload)
    svc_log.info(f"[control] ACK enviado al cliente\r\n")

    # 3) recibir artefacto (SIN parar monitor aún)
    if action == "upload_and_flash":
        size = int(header.get("artifact_size") or 0)
        svc_log.info(f"[control] recibiendo artefacto vía upload ({size} bytes)...\r\n")
        if size <= 0:
            raise ValueError("artifact_size inválido")
        remaining = size
        with artifact.open("wb") as f:
            while remaining > 0:
                chunk = sock.recv(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise ConnectionError("transferencia interrumpida")
                f.write(chunk); remaining -= len(chunk)
                if remaining % CHUNK_PROGRESS_INTERVAL == 0 or remaining < CHUNK_SIZE:
                    svc_log.info(f"[control] progreso: {size - remaining}/{size} bytes ({100*(size-remaining)//size}%)\r\n")
        svc_log.info(f"[control] artefacto recibido completamente: {artifact}\r\n")
        exp = header.get("artifact_sha256")
        if exp:
            svc_log.info(f"[control] verificando SHA256...\r\n")
            actual = sha256_file(artifact).lower()
            if actual != exp.lower():
                svc_log.error(f"[control] SHA256 esperado: {exp}, obtenido: {actual}\r\n")
                raise ValueError("hash SHA256 no coincide")
            svc_log.info(f"[control] SHA256 verificado OK\r\n")
    else:
        url = header.get("artifact_url")
        if not url:
            raise ValueError("falta artifact_url")
        svc_log.info(f"[control] descargando artefacto desde: {url}\r\n")
        req = Request(url, headers={"User-Agent":"remote-esp32/1.0"})
        downloaded = 0
        TIMEOUT_DOWNLOAD = 120
        with urlopen(req, timeout=TIMEOUT_DOWNLOAD) as r, artifact.open("wb") as f:
            while True:
                chunk = r.read(CHUNK_SIZE)
                if not chunk: break
                f.write(chunk)
                downloaded += len(chunk)
                if downloaded % CHUNK_PROGRESS_INTERVAL == 0:
                    svc_log.info(f"[control] descargado: {downloaded} bytes...\r\n")
        svc_log.info(f"[control] descarga completada: {downloaded} bytes → {artifact}\r\n")
        exp = header.get("artifact_sha256")
        if exp:
            svc_log.info(f"[control] verificando SHA256...\r\n")
            actual = sha256_file(artifact).lower()
            if actual != exp.lower():
                svc_log.error(f"[control] SHA256 esperado: {exp}, obtenido: {actual}\r\n")
                raise ValueError("hash SHA256 (pull) no coincide")
            svc_log.info(f"[control] SHA256 verificado OK\r\n")

    # 4) descomprimir y validar
    svc_log.info(f"[control] descomprimiendo {artifact}...\r\n")
    with zipfile.ZipFile(artifact,"r") as z:
        files = z.namelist()
        svc_log.info(f"[control] archivos en ZIP: {files}\r\n")
        z.extractall(jobdir)
    svc_log.info(f"[control] descompresión completada\r\n")
    if not (jobdir/"flasher_args.json").exists():
        svc_log.error(f"[control] flasher_args.json no encontrado en {jobdir}\r\n")
        raise FileNotFoundError("flasher_args.json no encontrado")

    chip = header.get("chip") or cfg["chip"]
    flash_baud = int(header.get("baud") or cfg["flash_baud"])
    encrypt = bool(header.get("encrypt", True))
    erase = bool(header.get("erase", False))
    svc_log.info(f"[control] parámetros flash → chip={chip}, baud={flash_baud}, encrypt={encrypt}, erase={erase}\r\n")

    daydir = logs_dir / time.strftime("%Y%m%d"); ensure_dir(daydir)
    job_log_path = daydir / f"{job_id}.log"
    svc_log.info(f"[control] log del job: {job_log_path}\r\n")
    job_log = logging.getLogger(f"job.{job_id}"); job_log.setLevel(logging.INFO)
    jfh = logging.FileHandler(job_log_path, encoding="utf-8")
    jfh.setFormatter(logging.Formatter("%(asctime)s | %(message)s","%Y-%m-%d %H:%M:%S"))
    job_log.addHandler(jfh)

    # 5) parar monitor → flashear → responder → relanzar
    # Establecer bandera para ignorar señales durante operación de flasheo
    _ignore_signals_flag.set()
    try:
        nprint("[flash] listo el artefacto → killing picocom...")
        svc_log.info("[flash] deteniendo monitor serial...\r\n")
        
        # Agregar logging antes de parar el monitor
        nprint("[flash] ANTES de parar monitor - proceso principal PID: " + str(os.getpid()))
        svc_log.info(f"[flash] ANTES de parar monitor - proceso principal PID: {os.getpid()}\r\n")
        
        try:
            mon.stop()
            nprint("[flash] monitor.stop() completado exitosamente")
            svc_log.info("[flash] monitor detenido\r\n")
            nprint("[flash] monitor detenido, continuando con flasheo...")
        except Exception as e:
            nprint(f"[flash] ERROR en mon.stop(): {e}")
            svc_log.error(f"[flash] ERROR en mon.stop(): {e}\r\n")
            raise

        try:
            esptool = find_esptool_cmd()
            svc_log.info(f"[flash] comando esptool: {' '.join(esptool)}\r\n")
            nprint(f"[flash] esptool encontrado: {' '.join(esptool)}")
            
            # Verificar si el ESP32 ya tiene flash encryption habilitado
            nprint("[flash] verificando estado de flash encryption...")
            check_cmd = esptool + ["--chip", chip, "--port", cfg["tty"], "flash_id"]
            try:
                result = subprocess.run(check_cmd, capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    nprint("[flash] ESP32 accesible para verificación")
                    # Si el ESP32 responde, asumir que puede tener flash encryption
                    # Para mayor seguridad, deshabilitar encrypt si hay errores
                    if encrypt:
                        nprint("[flash] WARNING: Flash encryption habilitado en header pero puede causar errores")
                        nprint("[flash] Si falla, intentar sin --encrypt")
                else:
                    nprint(f"[flash] WARNING: No se pudo verificar ESP32: {result.stderr}")
            except Exception as check_e:
                nprint(f"[flash] WARNING: Error verificando ESP32: {check_e}")
                
        except Exception as e:
            svc_log.error(f"[flash] esptool no encontrado: {e}\r\n")
            nprint(f"[flash] ERROR: esptool no encontrado: {e}")
            err = {"ok": False, "error": "esptool_not_found", "message": str(e)}
            payload = json.dumps(err).encode()
            sock.sendall(struct.pack(">I", len(payload)) + payload)
            return

        svc_log.info("[flash] construyendo comandos de flasheo...\r\n")
        nprint("[flash] construyendo comandos de flasheo...")
        
        try:
            erase_cmd, write_cmd, pairs = build_esptool_cmd(esptool, chip, cfg["tty"], flash_baud, encrypt, erase, jobdir)
            svc_log.info(f"[flash] archivos a flashear: {len(pairs)} pares\r\n")
            nprint(f"[flash] archivos a flashear: {len(pairs)} pares")
            for off, path in pairs:
                svc_log.info(f"  → {off}: {path}\r\n")
                nprint(f"  → {off}: {path}")
                
            # Verificar si falta archivo de aplicación
            offsets = [off for off, _ in pairs]
            missing_app = "0x10000" not in offsets and "0x120000" not in offsets
        except Exception as e:
            svc_log.error(f"[flash] ERROR construyendo comandos: {e}\r\n")
            nprint(f"[flash] ERROR construyendo comandos: {e}")
            err = {"ok": False, "error": "build_cmd_failed", "message": str(e)}
            payload = json.dumps(err).encode()
            sock.sendall(struct.pack(">I", len(payload)) + payload)
            return
        
        t0 = dt.datetime.now().isoformat()
        svc_log.info(f"[flash] inicio del flasheo: {t0}\r\n")
        nprint(f"[flash] inicio del flasheo: {t0}")
        
        rc_erase = 0
        if erase_cmd:
            svc_log.info("[flash] ejecutando erase_flash...\r\n")
            nprint("[flash] ejecutando erase_flash...")
            try:
                rc_erase = run_cmd(erase_cmd, job_log)
                svc_log.info(f"[flash] erase_flash terminado con código: {rc_erase}\r\n")
                nprint(f"[flash] erase_flash terminado con código: {rc_erase}")
            except Exception as e:
                svc_log.error(f"[flash] ERROR en erase_flash: {e}\r\n")
                nprint(f"[flash] ERROR en erase_flash: {e}")
                rc_erase = -1
        
        svc_log.info("[flash] ejecutando write_flash...\r\n")
        nprint("[flash] ejecutando write_flash...")
        try:
            rc_write = run_cmd(write_cmd, job_log)
            svc_log.info(f"[flash] write_flash terminado con código: {rc_write}\r\n")
            nprint(f"[flash] write_flash terminado con código: {rc_write}")
            
            # Si falla con código 2 y tiene --encrypt, intentar sin --encrypt
            if rc_write == 2 and encrypt:
                nprint("[flash] write_flash falló con código 2, intentando sin --encrypt...")
                svc_log.info("[flash] intentando write_flash sin --encrypt\r\n")
                
                # Reconstruir comando sin --encrypt
                write_cmd_no_encrypt = esptool + ["--chip", chip, "--port", cfg["tty"], "--baud", str(flash_baud),
                                                 "--before", "default-reset", "--after", "hard-reset",
                                                 "write-flash", "-z"]
                for off, p in pairs:
                    write_cmd_no_encrypt += [off, p]
                
                nprint("[flash] ejecutando write_flash SIN --encrypt...")
                try:
                    rc_write = run_cmd(write_cmd_no_encrypt, job_log)
                    svc_log.info(f"[flash] write_flash sin --encrypt terminado con código: {rc_write}\r\n")
                    nprint(f"[flash] write_flash sin --encrypt terminado con código: {rc_write}")
                except Exception as e:
                    svc_log.error(f"[flash] ERROR en write_flash sin --encrypt: {e}\r\n")
                    nprint(f"[flash] ERROR en write_flash sin --encrypt: {e}")
                    rc_write = -1
                    
        except Exception as e:
            svc_log.error(f"[flash] ERROR en write_flash: {e}\r\n")
            nprint(f"[flash] ERROR en write_flash: {e}")
            rc_write = -1
            
        t1 = dt.datetime.now().isoformat()
        svc_log.info(f"[flash] fin del flasheo: {t1}\r\n")

        ok = (rc_erase == 0) and (rc_write == 0)
        
        # Determinar tipo de resultado
        if ok:
            if missing_app:
                status_msg = "parcial (sin aplicación)"
                nprint("[flash] WARNING: Flasheo completado pero sin archivo de aplicación")
                nprint("[flash] El ESP32 necesitará ser flasheado con aplicación para funcionar")
            else:
                status_msg = "exitoso"
        else:
            status_msg = "fallido"
            
        svc_log.info(f"[flash] resultado: {status_msg} (erase={rc_erase}, write={rc_write})\r\n")
        nprint(f"[flash] resultado: {status_msg} (erase={rc_erase}, write={rc_write})")
        
        resp = {
            "ok": ok, "job_id": job_id, "started_at": t0, "finished_at": t1,
            "device": cfg["tty"], "chip": chip, "baud": flash_baud,
            "erase_rc": rc_erase, "write_rc": rc_write, "pairs": pairs,
            "log_file": str(job_log_path), "status": status_msg,
            "missing_app": missing_app
        }
        svc_log.info(f"[flash] enviando respuesta al cliente...\r\n")
        nprint("[flash] enviando respuesta al cliente...")
        payload = json.dumps(resp).encode()
        sock.sendall(struct.pack(">I", len(payload)) + payload)
        svc_log.info(f"[flash] respuesta enviada\r\n")
        nprint("[flash] respuesta enviada")
        nprint("="*60)
        nprint("[flash] FLASHEO COMPLETADO - reiniciando monitor serial...")
        nprint("="*60)
    except Exception as e:
        # Asegurar que la bandera se limpie incluso en caso de error crítico
        _ignore_signals_flag.clear()
        svc_log.exception(f"[flash] ERROR CRÍTICO: {e}\r\n")
        nprint(f"[flash] ERROR CRÍTICO: {e}")
        try:
            err = {"ok": False, "error": "flash_critical_error", "message": str(e)}
            payload = json.dumps(err).encode()
            sock.sendall(struct.pack(">I", len(payload)) + payload)
        except Exception:
            pass
    finally:
        # Limpiar bandera para volver a escuchar señales
        _ignore_signals_flag.clear()
        svc_log.info("[flash] reiniciando monitor serial...")
        nprint("[flash] reiniciando monitor serial...")
        try:
            mon.start()
            nprint("[flash] monitor reiniciado exitosamente")
        except Exception as e:
            nprint(f"[flash] ERROR al reiniciar monitor: {e}")
            svc_log.error(f"[flash] ERROR al reiniciar monitor: {e}\r\n")
            # Intentar una vez más después de un delay
            time.sleep(1)
            try:
                mon.start()
                nprint("[flash] monitor reiniciado en segundo intento")
            except Exception as e2:
                nprint(f"[flash] ERROR CRÍTICO: no se pudo reiniciar monitor: {e2}")
                svc_log.error(f"[flash] ERROR CRÍTICO: no se pudo reiniciar monitor: {e2}\r\n")

def control_server(cfg, mon: PicocomMonitor, svc_log: logging.Logger):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", cfg["port"]))
    srv.listen(TCP_BACKLOG)
    svc_log.info(f"[control] escuchando en 0.0.0.0:{cfg['port']} (tty={cfg['tty']})\r\n")
    nprint(f"[control] servidor TCP iniciado en puerto {cfg['port']}")
    
    while True:
        svc_log.info(f"[control] esperando conexión...\r\n")
        c, addr = srv.accept()
        nprint("─" * 60)
        nprint(f"[control] NUEVA CONEXIÓN desde {addr}")
        nprint("─" * 60)
        svc_log.info(f"[control] conexión aceptada desde {addr}\r\n")
        try:
            nprint(f"[control] ANTES de handle_control - PID: {os.getpid()}")
            handle_control(c, cfg, mon, svc_log)
            nprint(f"[control] DESPUÉS de handle_control - PID: {os.getpid()}")
            svc_log.info(f"[control] petición procesada exitosamente\r\n")
            nprint("[control] ✓ Petición completada")
        except Exception as e:
            nprint(f"[control] EXCEPCIÓN en control_server: {e}")
            svc_log.exception("error en control: %s", e)
            nprint(f"[control] ✗ ERROR: {e}")
            try:
                err = {"ok": False, "error": "exception", "message": str(e)}
                payload = json.dumps(err).encode()
                c.sendall(struct.pack(">I", len(payload)) + payload)
            except Exception:
                pass
        finally:
            try: 
                c.close()
                svc_log.info(f"[control] conexión cerrada\r\n")
            except Exception: 
                pass

# ========== main ==========

def main():
    ap = argparse.ArgumentParser(description="Monitor persistente con picocom + flasheo remoto")
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
    
    ensure_dir(logs_dir)
    ensure_dir(jobs_dir)

    svc_log = setup_logging(logs_dir)
    svc_log.info("========== INICIO DEL SERVICIO ==========")
    svc_log.info(f"TTY: {args.port_tty}")
    svc_log.info(f"Baud serial: {args.serial_baud}")
    svc_log.info(f"Puerto TCP: {args.control_port}")
    svc_log.info(f"Chip: {args.chip}")
    svc_log.info(f"Baud flash: {args.flash_baud}")
    svc_log.info(f"Token: {'configurado' if args.token else 'sin token'}")
    
    cfg = {
        "port": args.control_port,
        "tty": args.port_tty,
        "chip": args.chip,
        "flash_baud": args.flash_baud,
        "token": args.token,
        "logs_dir": logs_dir,
        "jobs_dir": jobs_dir
    }
    
    mon = PicocomMonitor(args.port_tty, args.serial_baud, logs_dir, cfg=cfg, svc_log=svc_log)
    svc_log.info("Iniciando monitor serial...\r\n")
    mon.start()

    svc_log.info("Iniciando servidor de control TCP...\r\n")
    th = threading.Thread(target=control_server, args=(cfg, mon, svc_log), daemon=True)
    th.start()
    svc_log.info("Servidor TCP iniciado en hilo daemon\r\n")

    nprint("[main] ✅ Sistema listo. Presiona Ctrl-C para salir.")
    nprint("[main] 💡 Presiona Ctrl-E para entrar en modo Erase Region")
    try:
        while not _shutdown_flag.is_set():
            # Usar timeout para verificar la bandera periódicamente
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

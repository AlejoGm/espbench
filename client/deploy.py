#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/deploy.py — Único comando: build + flash (local o remoto)
- Lee .flashcfg.json (gitignored)
- (Opcional) genera headers de versión
- Puede preguntar si hacer build+flash o solo flash
- Local: idf.py (encrypted-)flash (+ monitor opcional)
- Remoto: arma artifact.zip, lo envía y recibe JSON
"""

import argparse, json, os, pathlib, shlex, socket, struct, subprocess, sys, tempfile, zipfile, hashlib, time, shutil, platform, threading
import sys as _sys
_sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from common import sha256_file, send_msg, recv_msg

try:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.table import Table
    from rich import box as rich_box
    _console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    _console = None
    print("[WARN] 'rich' no instalado — output simplificado. Instalá con: cd client && ./install.sh")

# ---------------- utils ----------------

def find_idf_py(idf_py_config: str) -> str:
    """
    Busca idf.py en el PATH o usando variables de entorno de ESP-IDF.
    Retorna la ruta completa al ejecutable.
    """
    # Si ya es una ruta absoluta y existe, usarla directamente
    if os.path.isabs(idf_py_config) and os.path.isfile(idf_py_config):
        return idf_py_config
    
    # Si es solo "idf.py", buscar en PATH primero
    if idf_py_config == "idf.py" or "/" not in idf_py_config:
        idf_py_path = shutil.which("idf.py")
        if idf_py_path:
            return idf_py_path
    
    # Buscar usando variables de entorno comunes de ESP-IDF
    idf_paths = []
    for env_var in ["IDF_PATH", "ESP_IDF_PATH", "IDF_TOOLS_PATH"]:
        env_val = os.environ.get(env_var)
        if env_val:
            idf_paths.append(pathlib.Path(env_val))
    
    # Rutas comunes donde puede estar ESP-IDF
    common_paths = [
        pathlib.Path.home() / "esp" / "esp-idf",
        pathlib.Path.home() / ".espressif" / "esp-idf",
        pathlib.Path("/opt/esp/esp-idf"),
    ]
    idf_paths.extend(common_paths)
    
    # Buscar idf.py en cada ruta
    for idf_path in idf_paths:
        if idf_path and idf_path.exists():
            idf_py = idf_path / "tools" / "idf.py"
            if idf_py.exists():
                return str(idf_py)
    
    # Si no se encontró, retornar el valor original (fallará con mensaje claro)
    return idf_py_config

def auth_ping(host, port, token):
    print(f"[AUTH] Verificando conexión a {host}:{port}...")
    s = socket.create_connection((host, port), timeout=10)
    try:
        send_msg(s, {"token": str(token), "action": "xyz"})
        recv_msg(s)  # si falla acá, token/puerto/server mal
        print("[AUTH] ✓ Conexión exitosa")
        return True
    finally:
        s.close()

def run(cmd, cwd=None, env=None):
    print(">>", " ".join(shlex.quote(c) for c in (cmd if isinstance(cmd,list) else shlex.split(cmd))))
    try:
        subprocess.check_call(cmd, cwd=cwd, env=env)
    except FileNotFoundError as e:
        if cmd and len(cmd) > 0 and "idf.py" in str(cmd[0]):
            raise SystemExit(
                f"ERROR: No se pudo ejecutar '{cmd[0]}'.\n"
                f"El archivo no existe o no está en el PATH.\n"
                f"Verificá que ESP-IDF esté instalado y configurado correctamente."
            ) from e
        raise

def select_custom_files_macos() -> list:
    """
    Usa osascript (AppleScript) en macOS para seleccionar archivos.
    Permite seleccionar múltiples archivos uno por uno.
    Retorna una lista de rutas (strings) seleccionadas.
    """
    paths = []
    print("[CUSTOM] Seleccioná los archivos uno por uno (Cancelar para terminar)")
    
    # AppleScript para seleccionar un archivo a la vez
    script_template = '''try
    set theFile to choose file with prompt "{prompt}"
    return POSIX path of theFile
on error
    return ""
end try'''
    
    file_count = 0
    while True:
        file_count += 1
        if file_count == 1:
            prompt = "Seleccionar archivo custom (Cancelar para terminar)"
        else:
            prompt = f"Seleccionar archivo {file_count} (Cancelar para terminar, {len(paths)} archivo(s) seleccionado(s))"
        
        script = script_template.format(prompt=prompt)
        
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode != 0 or not result.stdout.strip():
                # Usuario canceló o hubo error
                break
            
            file_path = result.stdout.strip()
            if file_path and os.path.exists(file_path):
                paths.append(file_path)
                print(f"[CUSTOM] ✓ Archivo {file_count}: {pathlib.Path(file_path).name}")
            else:
                print(f"[CUSTOM] ⚠ Archivo no válido: {file_path}")
                break
        except subprocess.TimeoutExpired:
            print("[CUSTOM] ⚠ Timeout al seleccionar archivos")
            break
        except Exception as e:
            print(f"[CUSTOM] ⚠ Error: {e}")
            break
    
    return paths

def select_custom_files_windows() -> list:
    """
    Usa PowerShell en Windows para seleccionar archivos.
    Retorna una lista de rutas (strings) seleccionadas.
    """
    # PowerShell script para abrir diálogo de selección múltiple
    ps_script = '''
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "Seleccionar archivos custom para flashear"
    $dialog.Multiselect = $true
    $dialog.Filter = "Todos los archivos (*.*)|*.*|Archivos binarios (*.bin)|*.bin"
    $result = $dialog.ShowDialog()
    if ($result -eq "OK") {
        $dialog.FileNames | ForEach-Object { Write-Output $_ }
    }
    '''
    
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=300
        )
        
        if result.returncode != 0:
            print(f"[CUSTOM] ⚠ Error al ejecutar PowerShell: {result.stderr}")
            return []
        
        # Parsear las rutas (una por línea)
        paths = [p.strip() for p in result.stdout.strip().split('\n') if p.strip()]
        # Convertir rutas de Windows (con backslashes) a formato normalizado
        normalized_paths = []
        for p in paths:
            # PowerShell ya convierte backslashes a forward slashes en el script
            if p and os.path.exists(p):
                normalized_paths.append(str(pathlib.Path(p).resolve()))
        
        return normalized_paths
    except subprocess.TimeoutExpired:
        print("[CUSTOM] ⚠ Timeout al seleccionar archivos")
        return []
    except FileNotFoundError:
        # PowerShell no encontrado, usar tkinter como fallback
        print("[CUSTOM] ⚠ PowerShell no encontrado, usando tkinter como fallback...")
        return select_custom_files_tkinter()
    except Exception as e:
        print(f"[CUSTOM] ⚠ Error: {e}")
        return select_custom_files_tkinter()

def select_custom_files_tkinter() -> list:
    """
    Usa tkinter como fallback para seleccionar archivos.
    Funciona en Linux y como fallback en otros sistemas.
    """
    try:
        import tkinter
        from tkinter import filedialog
        
        root = tkinter.Tk()
        root.withdraw()
        try:
            root.attributes('-topmost', True)
        except:
            pass
        
        try:
            files = filedialog.askopenfilenames(
                title="Seleccionar archivos custom para flashear",
                filetypes=[("Todos los archivos", "*.*"), ("Archivos binarios", "*.bin"), ("Archivos", "*")]
            )
            return list(files) if files else []
        finally:
            root.destroy()
    except ImportError:
        raise SystemExit(
            "ERROR: No se puede abrir el explorador de archivos.\n"
            "Editá .custom_flash_files.json manualmente con las rutas de los archivos.\n"
            "Formato: {\"files\": [\"C:\\\\ruta\\\\archivo1.bin\", \"C:\\\\ruta\\\\archivo2.bin\"]}"
        )

def select_custom_files() -> list:
    """
    Abre el explorador de archivos para seleccionar archivos custom.
    Retorna una lista de rutas (strings) seleccionadas.
    Usa métodos nativos según el sistema operativo.
    """
    system = platform.system()
    
    if system == "Darwin":  # macOS
        print("[CUSTOM] Usando selector nativo de macOS...")
        print("[CUSTOM] Podés seleccionar múltiples archivos (Cancelar para terminar)")
        return select_custom_files_macos()
    elif system == "Windows":  # Windows
        print("[CUSTOM] Usando selector nativo de Windows...")
        return select_custom_files_windows()
    else:  # Linux y otros
        print("[CUSTOM] Usando tkinter...")
        return select_custom_files_tkinter()

def get_custom_files_config_path(project_root: pathlib.Path) -> pathlib.Path:
    """Retorna la ruta al archivo de configuración de archivos custom."""
    return project_root / ".custom_flash_files.json"

def select_custom_flasher_args() -> str:
    """
    Pide al usuario seleccionar un archivo flasher_args.json custom.
    Retorna la ruta al archivo seleccionado.
    """
    system = platform.system()
    
    if system == "Darwin":  # macOS
        print("[CUSTOM] Seleccioná el archivo flasher_args.json custom...")
        script = '''try
    set theFile to choose file with prompt "Seleccionar flasher_args.json custom" of type {"json", "public.json"}
    return POSIX path of theFile
on error
    return ""
end try'''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=300
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception as e:
            print(f"[CUSTOM] ⚠ Error: {e}")
    elif system == "Windows":  # Windows
        print("[CUSTOM] Seleccioná el archivo flasher_args.json custom...")
        ps_script = '''
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = "Seleccionar flasher_args.json custom"
        $dialog.Filter = "JSON files (*.json)|*.json|All files (*.*)|*.*"
        $result = $dialog.ShowDialog()
        if ($result -eq "OK") {
            Write-Output $dialog.FileName
        }
        '''
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=300
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception as e:
            print(f"[CUSTOM] ⚠ Error: {e}")
    else:  # Linux y otros
        print("[CUSTOM] Seleccioná el archivo flasher_args.json custom...")
        try:
            import tkinter
            from tkinter import filedialog
            
            root = tkinter.Tk()
            root.withdraw()
            try:
                root.attributes('-topmost', True)
            except:
                pass
            
            try:
                file_path = filedialog.askopenfilename(
                    title="Seleccionar flasher_args.json custom",
                    filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
                )
                return file_path if file_path else ""
            finally:
                root.destroy()
        except ImportError:
            pass
    
    # Fallback: pedir por línea de comandos
    print("\n[CUSTOM] Ingresá la ruta al archivo flasher_args.json custom:")
    file_path = input("Ruta: ").strip()
    return file_path if file_path else ""

def load_custom_flasher_args_path(project_root: pathlib.Path) -> str:
    """
    Carga la ruta al flasher_args.json custom desde .custom_flash_files.json.
    Retorna la ruta o cadena vacía si no existe.
    """
    config_path = get_custom_files_config_path(project_root)
    if not config_path.exists():
        return ""
    
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        flasher_args_path = data.get("flasher_args_path", "")
        if flasher_args_path:
            p = pathlib.Path(flasher_args_path)
            if p.exists() and p.is_file():
                return str(p.resolve())
            else:
                print(f"[CUSTOM] ⚠ Archivo flasher_args.json no encontrado: {flasher_args_path}")
        return ""
    except (json.JSONDecodeError, Exception) as e:
        print(f"[CUSTOM] ⚠ Error al leer {config_path}: {e}")
        return ""

def save_custom_flasher_args_path(project_root: pathlib.Path, flasher_args_path: str):
    """
    Guarda la ruta al flasher_args.json custom en .custom_flash_files.json.
    """
    config_path = get_custom_files_config_path(project_root)
    data = {"flasher_args_path": str(pathlib.Path(flasher_args_path).resolve())}
    config_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[CUSTOM] ✓ Configuración guardada en {config_path.name}")

def collect_artifact(build_dir: pathlib.Path, custom_flasher_args_path: str = None, is_custom_mode: bool = False, is_remote: bool = False)->pathlib.Path:
    print("[ARTIFACT] Recolectando archivos para flashear...")
    
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="artifact_"))
    out = tmpdir/"artifact.zip"
    print(f"[ARTIFACT] Creando ZIP en {out}")
    
    with zipfile.ZipFile(out,"w",compression=zipfile.ZIP_DEFLATED) as z:
        copied = set()
        
        def add(p: pathlib.Path, arcname: str = None):
            p = p.resolve()
            if p.is_file() and p not in copied:
                name = arcname if arcname else p.name
                z.write(p, arcname=name)
                copied.add(p)
                print(f"[ARTIFACT] + {name} ({p.stat().st_size} bytes)")
        
        if is_custom_mode and custom_flasher_args_path:
            # Modo custom: usar flasher_args.json custom
            print("[ARTIFACT] Modo custom: usando flasher_args.json custom")
            custom_fa = pathlib.Path(custom_flasher_args_path)
            if not custom_fa.exists():
                raise SystemExit(f"[CUSTOM] ✗ No existe el archivo flasher_args.json custom: {custom_flasher_args_path}")
            
            # Leer el flasher_args.json custom
            try:
                custom_data = json.loads(custom_fa.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise SystemExit(f"[CUSTOM] ✗ Error al leer flasher_args.json custom: {e}")
            
            # Copiar el flasher_args.json al ZIP
            z.write(custom_fa, arcname="flasher_args.json")
            print("[ARTIFACT] + flasher_args.json (custom)")
            
            # Agregar los archivos especificados en flash_files
            flash_files = custom_data.get("flash_files", {})
            if isinstance(flash_files, dict):
                print(f"[ARTIFACT] Procesando {len(flash_files)} archivo(s) desde flasher_args.json custom")
                for offset, file_path in flash_files.items():
                    if file_path:
                        fp = pathlib.Path(file_path)
                        # Si es ruta relativa, buscar en el mismo directorio que el flasher_args.json
                        if not fp.is_absolute():
                            fp = custom_fa.parent / fp
                        if fp.exists() and fp.is_file():
                            add(fp, arcname=fp.name)
                        else:
                            print(f"[ARTIFACT] ⚠ Archivo no encontrado: {fp}")
            
            # También procesar entradas individuales
            for key in ["bootloader", "app", "partition-table", "otadata"]:
                entry = custom_data.get(key)
                if isinstance(entry, dict):
                    file_path = entry.get("file")
                    if file_path:
                        fp = pathlib.Path(file_path)
                        if not fp.is_absolute():
                            fp = custom_fa.parent / fp
                        if fp.exists() and fp.is_file():
                            add(fp, arcname=fp.name)
        else:
            # Modo normal: usar flasher_args.json del build
            fa = build_dir / "flasher_args.json"
            if not fa.exists():
                raise SystemExit("no existe build/flasher_args.json (corré un build primero)")
            
            z.write(fa, arcname="flasher_args.json")
            print(f"[ARTIFACT] + flasher_args.json")
            J = json.loads(fa.read_text(encoding="utf-8"))
            
            # Procesar flash_files (puede ser dict o list)
            ff = J.get("flash_files")
            if isinstance(ff, dict):
                print(f"[ARTIFACT] flash_files es dict con {len(ff)} entradas")
                for offset, path in ff.items():
                    if path:
                        bp = pathlib.Path(path)
                        if not bp.is_absolute(): bp = build_dir / bp
                        add(bp)
            elif isinstance(ff, list):
                print(f"[ARTIFACT] flash_files es list con {len(ff)} entradas")
                for it in ff:
                    path = None
                    if isinstance(it, (list, tuple)) and len(it) >= 2:
                        path = it[1]
                    elif isinstance(it, dict):
                        path = it.get("file") or it.get("bin_file") or it.get("path")
                    if path:
                        bp = pathlib.Path(path)
                        if not bp.is_absolute(): bp = build_dir / bp
                        add(bp)
            
            # Procesar entradas individuales (bootloader, app, partition-table, otadata)
            for key in ["bootloader", "app", "partition-table", "otadata"]:
                entry = J.get(key)
                if isinstance(entry, dict):
                    path = entry.get("file")
                    if path:
                        bp = pathlib.Path(path)
                        if not bp.is_absolute(): bp = build_dir / bp
                        add(bp)
            
            # fallbacks por si acaso
            for rel in ["bootloader/bootloader.bin", "partition_table/partition-table.bin", "ota_data_initial.bin", "clc1.bin", "app.bin"]:
                p = build_dir / rel
                if p.exists():
                    add(p)

            if is_remote:
                elf_files = list(build_dir.glob("*.elf"))
                if elf_files:
                    z.write(elf_files[0], arcname="firmware.elf")
                    print(f"[ARTIFACT] + firmware.elf ({elf_files[0].stat().st_size} bytes)")
                else:
                    print("[ARTIFACT] WARNING: no .elf found in build dir, skipping")
    
    size_mb = out.stat().st_size / (1024*1024)
    print(f"[ARTIFACT] ✓ Artifact creado: {out.name} ({size_mb:.2f} MB)")
    return out

# ------------- multi-remote helpers -------------

_TEMPLATE_CFG = {
    "mode": "auto",
    "chip": "auto",
    "flash_baud": 921600,
    "encrypt": False,
    "erase": False,
    "paths": {
        "project_root": ".",
        "idf_py": "idf.py"
    },
    "remote": [
        {
            "name": "mi-board",
            "host": "sensipi01",
            "token": "",
            "lock_user": "tu-nombre",
            "lock_token": "token-secreto"
        }
    ]
}

def _generate_template(path: pathlib.Path):
    path.write_text(json.dumps(_TEMPLATE_CFG, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[CONFIG] Template generado en {path}")
    print("[CONFIG] Editá los valores y volvé a ejecutar.")
    sys.exit(0)

DASHBOARD_PORT = 8080


def _normalize_remotes(cfg) -> list:
    r = cfg.get("remote")
    if r is None:
        return []
    if isinstance(r, dict):
        return [r]
    if isinstance(r, list):
        return r
    return []

def _remote_name(r: dict) -> str:
    return r.get("name") or r.get("device_key") or r.get("host", "?")

def _resolve_device_port(r: dict) -> tuple:
    """Return (port_int, device_info_dict_or_None). Uses dashboard API if device_key present."""
    if "port" in r:
        return int(r["port"]), None
    device_key = r.get("device_key") or r.get("name")
    if not device_key:
        raise ValueError(f"Remote sin 'port' ni 'device_key'")
    host = r["host"]
    import urllib.request as _urllib
    url = f"http://{host}:{DASHBOARD_PORT}/api/device/by-key/{device_key}"
    try:
        with _urllib.urlopen(url, timeout=10) as resp:
            info = json.loads(resp.read())
        return int(info["port_tcp"]), info
    except Exception as e:
        raise RuntimeError(f"No se pudo resolver device '{device_key}' en {host}: {e}")

def _hw_model_from_build(build_dir: pathlib.Path) -> str:
    """Read hw_model from build/project_description.json (split on last '-')."""
    desc = build_dir / "project_description.json"
    if not desc.exists():
        return None
    try:
        data = json.loads(desc.read_text())
        pname = data.get("project_name", "")
        if not pname:
            return None
        idx = pname.rfind("-")
        return pname[:idx] if idx >= 0 else pname
    except Exception:
        return None

def flash_one(remote_cfg: dict, artifact: pathlib.Path, digest: str, size: int,
              job_id: str, chip: str, flash_baud: int, encrypt: bool, erase: bool,
              on_status=None, on_line=None, verbose: bool = False) -> dict:
    name = _remote_name(remote_cfg)
    logs = []

    def log(msg):
        logs.append(msg)
        if verbose:
            print(msg)

    def status(phase):
        if on_status:
            on_status(phase)
        log(f"  {phase}")

    lock_user  = str(remote_cfg.get("lock_user",  "")).strip()
    lock_token = str(remote_cfg.get("lock_token", "")).strip()
    if not lock_user or not lock_token:
        return {"ok": False, "name": name, "error": "falta lock_user/lock_token en config", "logs": logs}

    token = str(remote_cfg.get("token", ""))
    host  = remote_cfg["host"]
    port  = int(remote_cfg["port"])
    header = {
        "token": token, "action": "upload_and_flash",
        "job_id": f"{job_id}_{name}", "chip": chip, "baud": flash_baud,
        "encrypt": bool(encrypt), "erase": bool(erase),
        "artifact_size": size, "artifact_sha256": digest, "artifact_name": artifact.name,
        "lock_user": lock_user, "lock_token": lock_token,
        "stream": True,
    }
    try:
        status("conectando...")
        s = socket.create_connection((host, port), timeout=30)
        try:
            send_msg(s, header)
            status("esperando ACK...")
            ack = recv_msg(s)
            if not ack.get("ok") or ack.get("phase") != "ready":
                err = ack.get("message") or ack.get("error") or "ACK fallido"
                return {"ok": False, "name": name, "error": err, "logs": logs}
            status(f"enviando artifact ({size / (1024*1024):.1f} MB)...")
            with artifact.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    s.sendall(chunk)
            status("flasheando...")
            s.settimeout(300)
            stream_lines = []
            resp = None
            while True:
                msg = recv_msg(s)
                if "ok" in msg:  # mensaje final (server nuevo o viejo)
                    resp = msg
                    break
                if msg.get("phase") == "log":
                    line = msg.get("line", "")
                    stream_lines.append(line)
                    if verbose:
                        print(line, flush=True)
                    if on_line:
                        on_line(line)
            resp.setdefault("name", name)
            resp["logs"] = logs + stream_lines
            return resp
        finally:
            s.close()
    except Exception as e:
        return {"ok": False, "name": name, "error": str(e), "logs": logs}

def _flash_parallel(remotes: list, artifact: pathlib.Path, digest: str, size: int,
                    job_id: str, chip: str, flash_baud: int, encrypt: bool, erase: bool,
                    results_out: dict):
    results_lock = threading.Lock()

    _INTERESTING = ("Writing at", "Wrote", "Hash of", "Leaving", "Hard resetting",
                    "Compressed", "esptool", "Chip is", "WARNING", "ERROR")

    if HAS_RICH:
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=_console) as progress:
            task_ids = {_remote_name(r): progress.add_task(f"[cyan]{_remote_name(r)}[/cyan]  conectando...", total=None)
                        for r in remotes}

            def worker(r):
                n = _remote_name(r)
                def on_status(phase):
                    progress.update(task_ids[n], description=f"[cyan]{n}[/cyan]  {phase}")
                def on_line(line):
                    stripped = line.strip()
                    if any(kw in stripped for kw in _INTERESTING):
                        short = stripped[:70]
                        progress.update(task_ids[n], description=f"[cyan]{n}[/cyan]  {short}")
                result = flash_one(r, artifact, digest, size, job_id, chip, flash_baud, encrypt, erase,
                                   on_status=on_status, on_line=on_line)
                if result.get("ok"):
                    progress.update(task_ids[n], description=f"[green]✓ {n}[/green]")
                else:
                    err = result.get("error", "error")
                    progress.update(task_ids[n], description=f"[red]✗ {n}[/red]  {err}")
                with results_lock:
                    results_out[n] = result

            threads = [threading.Thread(target=worker, args=(r,)) for r in remotes]
            for t in threads: t.start()
            for t in threads: t.join()
    else:
        def worker(r):
            n = _remote_name(r)
            print(f"[{n}] iniciando...")
            def on_line(line):
                print(f"[{n}] {line}", flush=True)
            result = flash_one(r, artifact, digest, size, job_id, chip, flash_baud, encrypt, erase,
                               on_line=on_line)
            with results_lock:
                results_out[n] = result
            print(f"[{n}] {'✓ OK' if result.get('ok') else '✗ ' + result.get('error','error')}")

        threads = [threading.Thread(target=worker, args=(r,)) for r in remotes]
        for t in threads: t.start()
        for t in threads: t.join()

def _print_summary(results: dict):
    if HAS_RICH:
        table = Table(title="Resultado Flash", box=rich_box.ROUNDED, show_lines=False)
        table.add_column("Dispositivo", style="cyan", no_wrap=True)
        table.add_column("Estado", no_wrap=True)
        table.add_column("Detalle")
        for name, r in results.items():
            if r.get("ok"):
                detail = r.get("status", "exitoso")
                table.add_row(name, "[green]✓ OK[/green]", detail)
            else:
                err = r.get("error") or r.get("error_hint") or "error desconocido"
                table.add_row(name, "[red]✗ FAIL[/red]", err)
        _console.print(table)
        # Logs de los fallidos
        for name, r in results.items():
            if not r.get("ok") and r.get("logs"):
                _console.print(f"\n[yellow]── Log {name} ──[/yellow]")
                for line in r["logs"]:
                    _console.print(f"  {line}")
    else:
        print("\n=== RESUMEN ===")
        for name, r in results.items():
            mark = "✓" if r.get("ok") else "✗"
            detail = "" if r.get("ok") else f" — {r.get('error','')}"
            print(f"  {mark} {name}{detail}")

# ------------- flows -------------

def flash_remote(cfg, project_root: pathlib.Path, idf_py: str, encrypt: bool, erase: bool, chip: str, flash_baud: int, do_build: bool, custom_flasher_args_path: str = None, is_custom_mode: bool = False):
    remotes = _normalize_remotes(cfg)
    if not remotes:
        raise SystemExit("[REMOTE] ✗ No hay 'remote' en .flashcfg.json")

    build_dir = project_root / "build"
    if is_custom_mode:
        print("[CUSTOM] Modo custom: saltando build")
    elif do_build:
        print("[BUILD] Compilando proyecto...")
        run([idf_py, "build"], cwd=project_root)
    else:
        print("[BUILD] Saltando compilación (usando binarios existentes)")

    artifact = collect_artifact(build_dir, custom_flasher_args_path=custom_flasher_args_path,
                                is_custom_mode=is_custom_mode, is_remote=True)
    digest = sha256_file(artifact)
    size   = artifact.stat().st_size
    job_id = time.strftime("job_%Y%m%d_%H%M%S")

    # Resolver device_key → port y cross-check hw_model
    artifact_hw_model = _hw_model_from_build(build_dir)
    resolved = []
    for r in remotes:
        r = dict(r)
        if "port" not in r:
            try:
                port, device_info = _resolve_device_port(r)
                r["port"] = port
            except Exception as e:
                print(f"[ERROR] {_remote_name(r)}: {e}")
                continue
            if device_info and artifact_hw_model:
                dev_hw = device_info.get("hw_model")
                if dev_hw and dev_hw != artifact_hw_model:
                    name = _remote_name(r)
                    print(f"\n[WARN] HW model mismatch en '{name}':")
                    print(f"  artifact  = '{artifact_hw_model}'")
                    print(f"  device    = '{dev_hw}'")
                    if sys.stdin.isatty():
                        ans = input("¿Continuar de todas formas? [s/N] > ").strip().lower()
                        if ans not in ["s", "si", "y", "yes"]:
                            print(f"[SKIP] Saltando {name}")
                            continue
        resolved.append(r)
    remotes = resolved
    if not remotes:
        raise SystemExit("[REMOTE] ✗ No quedaron remotes válidos tras resolución")

    results = {}

    if len(remotes) == 1:
        r    = remotes[0]
        name = _remote_name(r)
        print(f"\n=== FLASH REMOTO → {name} ===")
        print(f"[REMOTE] Chip: {chip}, Baud: {flash_baud}, Encrypt: {encrypt}, Erase: {erase}")
        result = flash_one(r, artifact, digest, size, job_id, chip, flash_baud, encrypt, erase, verbose=True)
        results[name] = result
    else:
        print(f"\n=== FLASH REMOTO ({len(remotes)} dispositivos en paralelo) ===")
        _flash_parallel(remotes, artifact, digest, size, job_id, chip, flash_baud, encrypt, erase, results)

    _print_summary(results)

    failed_remotes = [r for r in remotes if not results.get(_remote_name(r), {}).get("ok")]
    if failed_remotes and sys.stdin.isatty():
        names = ", ".join(_remote_name(r) for r in failed_remotes)
        retry = input(f"\n¿Reintentar {len(failed_remotes)} dispositivo(s) fallido(s) sin build? [{names}] [s/N] > ").strip().lower()
        if retry in ["s", "si", "y", "yes"]:
            print("\n[RETRY] Reintentando...")
            retry_results = {}
            if len(failed_remotes) == 1:
                r    = failed_remotes[0]
                name = _remote_name(r)
                retry_results[name] = flash_one(r, artifact, digest, size, job_id + "_retry",
                                                chip, flash_baud, encrypt, erase, verbose=True)
            else:
                _flash_parallel(failed_remotes, artifact, digest, size, job_id + "_retry",
                                chip, flash_baud, encrypt, erase, retry_results)
            results.update(retry_results)
            _print_summary(results)

    return 0 if all(r.get("ok") for r in results.values()) else 1

def unlock_remote(cfg):
    print("\n=== UNLOCK REMOTO ===")
    remotes = _normalize_remotes(cfg)
    if not remotes:
        raise SystemExit("[UNLOCK] ✗ No hay 'remote' en .flashcfg.json")

    results_lock = threading.Lock()
    results = {}

    def unlock_one(r):
        r = dict(r)
        name       = _remote_name(r)
        lock_user  = str(r.get("lock_user",  "")).strip()
        lock_token = str(r.get("lock_token", "")).strip()
        if not lock_user or not lock_token:
            with results_lock:
                results[name] = {"ok": False, "name": name, "error": "falta lock_user/lock_token"}
            return
        if "port" not in r:
            try:
                port_resolved, _ = _resolve_device_port(r)
                r["port"] = port_resolved
            except Exception as e:
                with results_lock:
                    results[name] = {"ok": False, "name": name, "error": str(e)}
                return
        token = str(r.get("token", ""))
        host  = r["host"]
        port  = int(r["port"])
        try:
            s = socket.create_connection((host, port), timeout=30)
            try:
                send_msg(s, {"token": token, "action": "unlock",
                             "lock_user": lock_user, "lock_token": lock_token})
                resp = recv_msg(s)
                resp.setdefault("name", name)
            finally:
                s.close()
        except Exception as e:
            resp = {"ok": False, "name": name, "error": str(e)}
        with results_lock:
            results[name] = resp

    threads = [threading.Thread(target=unlock_one, args=(r,)) for r in remotes]
    for t in threads: t.start()
    for t in threads: t.join()

    _print_summary(results)
    return 0 if all(r.get("ok") for r in results.values()) else 1


def flash_local(cfg, project_root: pathlib.Path, idf_py: str, encrypt: bool, erase: bool, chip: str, flash_baud: int, do_build: bool):
    print("\n=== FLASH LOCAL ===")
    port = cfg["local"]["port"]
    print(f"[LOCAL] Puerto: {port}")
    print(f"[LOCAL] Chip: {chip}, Baud: {flash_baud}, Encrypt: {encrypt}, Erase: {erase}")
    
    if do_build:
        print("[BUILD] Compilando proyecto...")
        run([idf_py, "build"], cwd=project_root)
    else:
        print("[BUILD] Saltando compilación (usando binarios existentes)")

    # erase opcional
    if erase:
        print("[LOCAL] Borrando flash...")
        try:
            run([idf_py, "-p", port, "erase-flash"], cwd=project_root)
            print("[LOCAL] ✓ Flash borrado")
        except subprocess.CalledProcessError:
            print("[LOCAL] ⚠ Error al borrar flash (continuando)")

    target = "encrypted-flash" if encrypt else "write_flash"
    print(f"[LOCAL] Flasheando ({target})...")
    base_cmd = [idf_py, "-p", port, target]
    if chip != "auto":
        base_cmd += ["--chip", chip]
    if flash_baud:
        base_cmd += ["-b", str(flash_baud)]

    run(base_cmd, cwd=project_root)
    print("[LOCAL] ✓ Flash completado")
    
    if cfg.get("local", {}).get("monitor", True):
        print("[LOCAL] Iniciando monitor serial...")
        run([idf_py, "-p", port, "monitor"], cwd=project_root)
    return 0

# ------------- main -------------

def main():
    print("=== DEPLOY TOOL ===\n")
    ap = argparse.ArgumentParser(description="Build + flash (local o remoto) con config gitignored")
    ap.add_argument("--cfg", default=".flashcfg.json")
    ap.add_argument("--unlock", action="store_true", help="liberar lock del dispositivo remoto")
    ap.add_argument("--mode", choices=["auto","local","remote"], default=None)
    ap.add_argument("--no-version", action="store_true")
    ap.add_argument("--no-build", action="store_true", help="no hacer build (equivalente a elegir 'solo flash')")
    ap.add_argument("--no-encrypt", dest="encrypt", action="store_false", default=None)
    ap.add_argument("--erase", action="store_true", default=None)
    ap.add_argument("--ask", action="store_true", help="preguntar si build+flash o solo flash")
    ap.add_argument("--custom", action="store_true", help="usar archivos custom para flasheo remoto")
    args = ap.parse_args()

    cfg_path = pathlib.Path(args.cfg)
    print(f"[CONFIG] Cargando configuración desde {cfg_path}")
    if not cfg_path.exists():
        print(f"[CONFIG] No existe {cfg_path} — generando template...")
        _generate_template(cfg_path)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    print(f"[CONFIG] ✓ Configuración cargada")

    if args.unlock:
        sys.exit(unlock_remote(cfg))

    mode = args.mode or cfg.get("mode","auto")
    paths = cfg.get("paths", {})
    project_root = pathlib.Path(paths.get("project_root",".")).resolve()
    idf_py_config = paths.get("idf_py","idf.py")
    idf_py = find_idf_py(idf_py_config)
    
    print(f"[CONFIG] Modo: {mode}")
    print(f"[CONFIG] Proyecto: {project_root}")
    print(f"[CONFIG] IDF.PY: {idf_py}")
    
    # Verificar que idf.py existe y es ejecutable
    if not os.path.isfile(idf_py):
        raise SystemExit(
            f"ERROR: No se encontró idf.py en '{idf_py}'.\n"
            f"Opciones:\n"
            f"  1. Asegurate de tener ESP-IDF instalado y el entorno cargado\n"
            f"  2. Especificá la ruta completa en .flashcfg.json: \"paths\": {{\"idf_py\": \"/ruta/completa/a/idf.py\"}}\n"
            f"  3. O especificá la ruta a ESP-IDF: \"paths\": {{\"idf_py\": \"$IDF_PATH/tools/idf.py\"}}"
        )

    # version headers
    if not args.no_version:
        gvs = paths.get("git_version_script","")
        if gvs:
            print("[VERSION] Generando headers de versión...")
            run([sys.executable, gvs], cwd=project_root)
        else:
            vh = project_root/"tools"/"version_headers.py"
            if vh.exists():
                print("[VERSION] Generando headers de versión...")
                run([sys.executable, str(vh), str(project_root)], cwd=project_root)

    encrypt = cfg.get("encrypt", True) if args.encrypt is None else args.encrypt
    erase = cfg.get("erase", False) if args.erase is None else args.erase
    chip = cfg.get("chip","auto")
    flash_baud = int(cfg.get("flash_baud", 921600))

    # Manejar modo custom (solo para modo remoto)
    custom_flasher_args_path = None
    is_custom_mode = False
    if args.custom:
        if mode == "local" or (mode == "auto" and "remote" not in cfg):
            raise SystemExit("[CUSTOM] ⚠ --custom solo funciona en modo remoto")
        
        print("[CUSTOM] Modo custom activado")
        config_path = get_custom_files_config_path(project_root)
        
        if config_path.exists():
            custom_flasher_args_path = load_custom_flasher_args_path(project_root)
            if custom_flasher_args_path:
                print(f"[CUSTOM] ✓ Usando flasher_args.json custom: {pathlib.Path(custom_flasher_args_path).name}")
            else:
                print(f"[CUSTOM] ⚠ No se encontró flasher_args.json custom en {config_path.name}")
                print("[CUSTOM] Seleccionando flasher_args.json custom...")
                selected_path = select_custom_flasher_args()
                if selected_path:
                    save_custom_flasher_args_path(project_root, selected_path)
                    custom_flasher_args_path = selected_path
        else:
            print(f"[CUSTOM] No existe {config_path.name}, seleccionando flasher_args.json custom...")
            selected_path = select_custom_flasher_args()
            if selected_path:
                save_custom_flasher_args_path(project_root, selected_path)
                custom_flasher_args_path = selected_path
        
        if not custom_flasher_args_path:
            raise SystemExit("[CUSTOM] ✗ No se seleccionó flasher_args.json custom. Abortando.")
        
        is_custom_mode = True

    # ¿build+flash o solo flash? (solo si no es modo custom)
    do_build = not args.no_build and not is_custom_mode
    if args.ask and not args.no_build and not is_custom_mode:
        choice = input("\n¿Qué querés hacer? [1] build+flash  [2] solo flash  > ").strip()
        if choice == "2":
            do_build = False
            print("[CONFIG] Modo: solo flash")

    if mode == "local":
        if args.custom:
            print("[CUSTOM] ⚠ --custom solo funciona en modo remoto, se ignorará")
        print(f"[CONFIG] ✓ Usando modo LOCAL")
        exitc = flash_local(cfg, project_root, idf_py, encrypt, erase, chip, flash_baud, do_build)
    elif mode == "remote":
        print(f"[CONFIG] ✓ Usando modo REMOTO")
        exitc = flash_remote(cfg, project_root, idf_py, encrypt, erase, chip, flash_baud, do_build, custom_flasher_args_path=custom_flasher_args_path, is_custom_mode=is_custom_mode)
    else:  # auto
        if _normalize_remotes(cfg):
            print(f"[CONFIG] ✓ Modo AUTO detectó configuración remota")
            exitc = flash_remote(cfg, project_root, idf_py, encrypt, erase, chip, flash_baud, do_build, custom_flasher_args_path=custom_flasher_args_path, is_custom_mode=is_custom_mode)
        else:
            if args.custom:
                print("[CUSTOM] ⚠ --custom solo funciona en modo remoto, se ignorará")
            print(f"[CONFIG] ✓ Modo AUTO usando LOCAL")
            exitc = flash_local(cfg, project_root, idf_py, encrypt, erase, chip, flash_baud, do_build)

    # Si falló, preguntar si se quiere reintentar sin hacer build
    if exitc != 0:
        print(f"\n[DEPLOY] ✗ Falló (código: {exitc})")
        if do_build and sys.stdin.isatty():  # Solo preguntar si se hizo build y hay terminal interactiva
            try:
                retry = input("\n¿Reintentar flash sin hacer build? [s/N] > ").strip().lower()
                if retry in ['s', 'sí', 'si', 'y', 'yes']:
                    print("\n[RETRY] Reintentando flash sin build...")
                    if mode == "local":
                        exitc = flash_local(cfg, project_root, idf_py, encrypt, erase, chip, flash_baud, False)
                    elif mode == "remote":
                        exitc = flash_remote(cfg, project_root, idf_py, encrypt, erase, chip, flash_baud, False, custom_flasher_args_path=custom_flasher_args_path, is_custom_mode=is_custom_mode)
                    else:  # auto
                        if _normalize_remotes(cfg):
                            exitc = flash_remote(cfg, project_root, idf_py, encrypt, erase, chip, flash_baud, False, custom_flasher_args_path=custom_flasher_args_path, is_custom_mode=is_custom_mode)
                        else:
                            exitc = flash_local(cfg, project_root, idf_py, encrypt, erase, chip, flash_baud, False)
                    print(f"\n[DEPLOY] {'✓ Exitoso' if exitc == 0 else '✗ Falló'} (código: {exitc})")
                else:
                    print("[RETRY] Reintento cancelado")
            except (EOFError, KeyboardInterrupt):
                # No hay entrada disponible o se canceló manualmente
                print("[RETRY] No se puede preguntar (entrada no disponible)")
        elif do_build:
            # Se hizo build pero no hay terminal interactiva
            print("[INFO] Para reintentar sin build, ejecutá: python tools/deploy.py --no-build")
    else:
        print(f"\n[DEPLOY] ✓ Exitoso (código: {exitc})")
    
    sys.exit(exitc)

if __name__ == "__main__":
    main()
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

import argparse, json, os, pathlib, shlex, socket, struct, subprocess, sys, tempfile, zipfile, hashlib, time, shutil, platform
import sys as _sys
_sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from common import sha256_file, send_msg, recv_msg

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

# ------------- flows -------------

def flash_remote(cfg, project_root: pathlib.Path, idf_py: str, encrypt: bool, erase: bool, chip: str, flash_baud: int, do_build: bool, custom_flasher_args_path: str = None, is_custom_mode: bool = False):
    print("\n=== FLASH REMOTO ===")
    
    build_dir = project_root / "build"
    
    if is_custom_mode:
        print("[CUSTOM] Modo custom: saltando build")
    elif do_build:
        print("[BUILD] Compilando proyecto...")
        run([idf_py, "build"], cwd=project_root)
    else:
        print("[BUILD] Saltando compilación (usando binarios existentes)")

    artifact = collect_artifact(build_dir, custom_flasher_args_path=custom_flasher_args_path, is_custom_mode=is_custom_mode, is_remote=True)
    digest = sha256_file(artifact)
    size = artifact.stat().st_size
    job_id = time.strftime("job_%Y%m%d_%H%M%S")

    token = str(cfg["remote"].get("token",""))
    lock_user = str(cfg["remote"].get("lock_user","")).strip()
    lock_token = str(cfg["remote"].get("lock_token","")).strip()
    if not lock_user or not lock_token:
        raise SystemExit(
            "[REMOTE] ✗ Falta 'lock_user' y/o 'lock_token' en .flashcfg.json > remote.\n"
            "  Agregá: \"lock_user\": \"alejo\", \"lock_token\": \"token-secreto\""
        )
    header = {
        "token": token,
        "action": "upload_and_flash",
        "job_id": job_id,
        "chip": chip,
        "baud": flash_baud,
        "encrypt": bool(encrypt),
        "erase": bool(erase),
        "artifact_size": size,
        "artifact_sha256": digest,
        "artifact_name": artifact.name,
        "lock_user": lock_user,
        "lock_token": lock_token,
    }

    host = cfg["remote"]["host"]; port = int(cfg["remote"]["port"])
    token = str(cfg["remote"].get("token",""))

    print(f"[REMOTE] Conectando a {host}:{port}...")
    print(f"[REMOTE] Job ID: {job_id}")
    print(f"[REMOTE] Chip: {chip}, Baud: {flash_baud}, Encrypt: {encrypt}, Erase: {erase}")
    s = socket.create_connection((host, port), timeout=30)
    print("[REMOTE] ✓ Conexión establecida")
    try:
        # 1) header
        print("[REMOTE] Enviando header...")
        send_msg(s, header)

        # 2) ACK
        print("[REMOTE] Esperando ACK del servidor...")
        ack = recv_msg(s)
        if not ack.get("ok") or ack.get("phase") != "ready":
            print("[REMOTE] ✗ ERROR (ACK):", json.dumps(ack, indent=2, ensure_ascii=False))
            return 1
        print("[REMOTE] ✓ Servidor listo para recibir artifact")

        # 3) enviar ZIP
        print(f"[REMOTE] Enviando artifact ({size / (1024*1024):.2f} MB)...")
        sent_bytes = 0
        chunk_size = 1024*1024
        with artifact.open("rb") as f:
            for chunk in iter(lambda:f.read(chunk_size), b""):
                s.sendall(chunk)
                sent_bytes += len(chunk)
                progress = (sent_bytes / size) * 100
                print(f"[REMOTE] Progreso: {progress:.1f}% ({sent_bytes / (1024*1024):.2f} MB)")
        print("[REMOTE] ✓ Artifact enviado completamente")

        # 4) respuesta final (puede tardar varios minutos)
        print("[REMOTE] Esperando resultado del flasheo (esto puede tardar varios minutos)...")
        s.settimeout(300)  # 5 minutos para el flasheo
        resp = recv_msg(s)

        print("\n=== RESULTADO FLASH REMOTO ===")
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        if resp.get("ok"):
            print("[REMOTE] ✓ Flash completado exitosamente")
        else:
            hint = resp.get("error_hint", "")
            write_rc = resp.get("write_rc")
            print(f"[REMOTE] ✗ Flash falló (write_rc={write_rc}){': ' + hint if hint else ''}")
        return 0 if resp.get("ok") else 1
    finally:
        s.close()
        print("[REMOTE] Conexión cerrada")

def unlock_remote(cfg):
    print("\n=== UNLOCK REMOTO ===")
    lock_user = str(cfg["remote"].get("lock_user","")).strip()
    lock_token = str(cfg["remote"].get("lock_token","")).strip()
    if not lock_user or not lock_token:
        raise SystemExit("[UNLOCK] ✗ Falta 'lock_user' y/o 'lock_token' en .flashcfg.json > remote.")
    token = str(cfg["remote"].get("token",""))
    host = cfg["remote"]["host"]
    port = int(cfg["remote"]["port"])
    print(f"[UNLOCK] Conectando a {host}:{port} como '{lock_user}'...")
    s = socket.create_connection((host, port), timeout=30)
    try:
        send_msg(s, {"token": token, "action": "unlock", "lock_user": lock_user, "lock_token": lock_token})
        resp = recv_msg(s)
        if resp.get("ok"):
            print(f"[UNLOCK] ✓ {resp.get('message', 'desbloqueado')}")
        else:
            print(f"[UNLOCK] ✗ {resp.get('error')}: {resp.get('message','')}")
        return 0 if resp.get("ok") else 1
    finally:
        s.close()


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
        example = cfg_path.resolve().parent / ".flashcfg.json.example"
        hint = f" Copiá {example.name} a {cfg_path.name} y editá los valores." if example.exists() else ""
        raise SystemExit(f"No existe {cfg_path}. Creá .flashcfg.json (gitignored).{hint}")
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
        if "remote" in cfg and cfg["remote"].get("host") and cfg["remote"].get("port"):
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
                        if "remote" in cfg and cfg["remote"].get("host") and cfg["remote"].get("port"):
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
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
protocol.py — Protocolo TCP de control para flasheo remoto de ESP32.

Contiene el servidor TCP y el handler de conexiones de control.
Importado por remote_esp32.py como thin entrypoint.
"""

import datetime as dt, json, logging, os, pathlib, shutil, socket, struct, subprocess, sys, tempfile, threading, time, zipfile
from urllib.request import urlopen, Request

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))
from common import sha256_file, send_msg, recv_msg
from flash import find_esptool_cmd, build_esptool_cmd, run_cmd
from monitor import _ignore_signals_flag, nprint, EspMonitor

# ========== Constantes ==========
CHUNK_SIZE = 1024 * 1024          # 1 MB
CHUNK_PROGRESS_INTERVAL = 5 * CHUNK_SIZE  # Log cada 5 MB
TCP_BACKLOG = 5
TIMEOUT_DOWNLOAD = 120            # segundos


def ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)


def handle_control(sock, cfg, mon: EspMonitor, svc_log: logging.Logger):
    nprint(f"[control] handle_control iniciado - PID: {os.getpid()}")
    svc_log.info(f"[control] handle_control iniciado - PID: {os.getpid()}\r\n")

    # 1) header
    svc_log.info("[control] recibiendo header...\r\n")
    header = recv_msg(sock)
    svc_log.info(f"[control] header recibido: {json.dumps(header, indent=2)}\r\n")

    token = str(cfg["token"] or "")
    if token and header.get("token") != token:
        svc_log.warning("[control] token inválido, rechazando conexión\r\n")
        send_msg(sock, {"ok": False, "error": "unauthorized"})
        return

    action = header.get("action")
    stream = bool(header.get("stream", False))
    svc_log.info(f"[control] acción solicitada: {action}, stream={stream}\r\n")
    if action not in ("upload_and_flash", "pull_and_flash", "unlock"):
        svc_log.error(f"[control] acción inválida: {action}")
        send_msg(sock, {"ok": False, "error": "bad_action"})
        return

    def stream_line(line: str):
        if not stream:
            return
        try:
            send_msg(sock, {"phase": "log", "line": line})
        except Exception:
            pass

    # Lock check
    lock_user = header.get("lock_user", "").strip()
    lock_token = header.get("lock_token", "").strip()
    tty_name = os.path.basename(cfg["tty"])
    locks_dir = pathlib.Path(cfg.get("base", "/opt/esp")) / "locks"
    lock_file = locks_dir / tty_name

    def _read_lock():
        parts = lock_file.read_text().strip().split(':', 1)
        return parts[0], parts[1] if len(parts) > 1 else ''

    if action == "unlock":
        if not lock_user or not lock_token:
            send_msg(sock, {"ok": False, "error": "lock_credentials_required"})
            return
        if lock_file.exists():
            stored_user, stored_token = _read_lock()
            if stored_user != lock_user or stored_token != lock_token:
                send_msg(sock, {"ok": False, "error": "token_mismatch",
                                "message": "Par user/token incorrecto"})
                return
            lock_file.unlink()
        send_msg(sock, {"ok": True, "message": "desbloqueado"})
        return

    if not lock_user or not lock_token:
        svc_log.warning("[control] lock_user/lock_token ausente, rechazando\r\n")
        send_msg(sock, {"ok": False, "error": "lock_credentials_required",
                        "message": "Configurá 'lock_user' y 'lock_token' en .flashcfg.json > remote"})
        return

    if lock_file.exists():
        stored_user, stored_token = _read_lock()
        if stored_user != lock_user:
            svc_log.warning(f"[control] dispositivo bloqueado por '{stored_user}', rechazando '{lock_user}'\r\n")
            send_msg(sock, {"ok": False, "error": "device_locked",
                            "message": f"Dispositivo bloqueado por '{stored_user}'"})
            return
        if stored_token != lock_token:
            svc_log.warning(f"[control] token incorrecto para '{lock_user}'\r\n")
            send_msg(sock, {"ok": False, "error": "token_mismatch",
                            "message": "Token incorrecto"})
            return

    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(f"{lock_user}:{lock_token}")
    try:
        lock_file.chmod(0o666)
    except Exception:
        pass
    svc_log.info(f"[control] lock adquirido por '{lock_user}'\r\n")
    nprint(f"[control] lock adquirido por '{lock_user}'")

    # 2) preparar job + ACK
    job_id = header.get("job_id") or time.strftime("job_%Y%m%d_%H%M%S")
    svc_log.info(f"[control] job_id: {job_id}\r\n")
    jobs_dir: pathlib.Path = cfg["jobs_dir"]
    logs_dir: pathlib.Path = cfg["logs_dir"]
    jobdir = jobs_dir / job_id; ensure_dir(jobdir)
    svc_log.info(f"[control] directorio de trabajo: {jobdir}\r\n")
    artifact = jobdir / "artifact.zip"

    send_msg(sock, {"ok": True, "phase": "ready", "job_id": job_id})
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
        req = Request(url, headers={"User-Agent": "remote-esp32/1.0"})
        downloaded = 0
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
    with zipfile.ZipFile(artifact, "r") as z:
        files = z.namelist()
        svc_log.info(f"[control] archivos en ZIP: {files}\r\n")
        z.extractall(jobdir)
    svc_log.info(f"[control] descompresión completada\r\n")

    elf_src = jobdir / "firmware.elf"
    if elf_src.exists():
        base_dir = pathlib.Path(cfg.get("base", "/opt/esp"))
        elf_dst = base_dir / "current.elf"
        shutil.copy2(elf_src, elf_dst)
        svc_log.info(f"[control] firmware.elf → {elf_dst}\r\n")

    if not (jobdir / "flasher_args.json").exists():
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
    jfh.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    job_log.addHandler(jfh)

    # 5) parar monitor → flashear → responder → relanzar
    # Establecer bandera para ignorar señales durante operación de flasheo
    _ignore_signals_flag.set()
    try:
        nprint("[flash] listo el artefacto → deteniendo monitor...")
        svc_log.info("[flash] deteniendo monitor serial...\r\n")

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

            nprint("[flash] verificando estado de flash encryption...")
            check_cmd = esptool + ["--chip", chip, "--port", cfg["tty"], "flash_id"]
            try:
                result = subprocess.run(check_cmd, capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    nprint("[flash] ESP32 accesible para verificación")
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
            send_msg(sock, {"ok": False, "error": "esptool_not_found", "message": str(e)})
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

            offsets = [off for off, _ in pairs]
            missing_app = "0x10000" not in offsets and "0x120000" not in offsets
        except Exception as e:
            svc_log.error(f"[flash] ERROR construyendo comandos: {e}\r\n")
            nprint(f"[flash] ERROR construyendo comandos: {e}")
            send_msg(sock, {"ok": False, "error": "build_cmd_failed", "message": str(e)})
            return

        t0 = dt.datetime.now().isoformat()
        svc_log.info(f"[flash] inicio del flasheo: {t0}\r\n")
        nprint(f"[flash] inicio del flasheo: {t0}")

        stream_line(f"[espbench] artifact OK — {len(pairs)} archivos a flashear")
        for off, path in pairs:
            stream_line(f"[espbench]   {off}: {pathlib.Path(path).name}")

        rc_erase = 0
        if erase_cmd:
            svc_log.info("[flash] ejecutando erase_flash...\r\n")
            nprint("[flash] ejecutando erase_flash...")
            stream_line("[espbench] erase_flash...")
            try:
                rc_erase = run_cmd(erase_cmd, job_log, on_line=stream_line)
                svc_log.info(f"[flash] erase_flash terminado con código: {rc_erase}\r\n")
                nprint(f"[flash] erase_flash terminado con código: {rc_erase}")
            except Exception as e:
                svc_log.error(f"[flash] ERROR en erase_flash: {e}\r\n")
                nprint(f"[flash] ERROR en erase_flash: {e}")
                rc_erase = -1

        svc_log.info("[flash] ejecutando write_flash...\r\n")
        nprint("[flash] ejecutando write_flash...")
        stream_line("[espbench] write_flash...")
        try:
            rc_write = run_cmd(write_cmd, job_log, on_line=stream_line)
            svc_log.info(f"[flash] write_flash terminado con código: {rc_write}\r\n")
            nprint(f"[flash] write_flash terminado con código: {rc_write}")

            if rc_write == 2 and encrypt:
                nprint("[flash] write_flash falló con código 2, intentando sin --encrypt...")
                svc_log.info("[flash] intentando write_flash sin --encrypt\r\n")
                stream_line("[espbench] reintentando sin --encrypt...")

                write_cmd_no_encrypt = esptool + ["--chip", chip, "--port", cfg["tty"], "--baud", str(flash_baud),
                                                  "--before", "default-reset", "--after", "hard-reset",
                                                  "write-flash", "-z"]
                for off, p in pairs:
                    write_cmd_no_encrypt += [off, p]

                nprint("[flash] ejecutando write_flash SIN --encrypt...")
                try:
                    rc_write = run_cmd(write_cmd_no_encrypt, job_log, on_line=stream_line)
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

        _ESPTOOL_RC_HINTS = {
            0:  "OK",
            1:  "Error general de esptool.",
            2:  "Error fatal de conexión: boot mode incorrecto, chip no responde o puerto ocupado.",
            -1: "Error interno al lanzar esptool.",
        }

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
            "missing_app": missing_app,
        }
        if not ok:
            failing_rc = rc_write if rc_write != 0 else rc_erase
            resp["error_hint"] = _ESPTOOL_RC_HINTS.get(failing_rc, f"exit code {failing_rc} desconocido.")
        svc_log.info(f"[flash] enviando respuesta al cliente...\r\n")
        nprint("[flash] enviando respuesta al cliente...")
        send_msg(sock, {**resp, "phase": "done"})
        svc_log.info(f"[flash] respuesta enviada\r\n")
        nprint("[flash] respuesta enviada")
        nprint("=" * 60)
        nprint("[flash] FLASHEO COMPLETADO - reiniciando monitor serial...")
        nprint("=" * 60)
    except Exception as e:
        _ignore_signals_flag.clear()
        svc_log.exception(f"[flash] ERROR CRÍTICO: {e}\r\n")
        nprint(f"[flash] ERROR CRÍTICO: {e}")
        try:
            send_msg(sock, {"ok": False, "error": "flash_critical_error", "message": str(e)})
        except Exception:
            pass
    finally:
        _ignore_signals_flag.clear()
        svc_log.info("[flash] reiniciando monitor serial...")
        nprint("[flash] reiniciando monitor serial...")
        try:
            mon.start()
            nprint("[flash] monitor reiniciado exitosamente")
        except Exception as e:
            nprint(f"[flash] ERROR al reiniciar monitor: {e}")
            svc_log.error(f"[flash] ERROR al reiniciar monitor: {e}\r\n")
            time.sleep(1)
            try:
                mon.start()
                nprint("[flash] monitor reiniciado en segundo intento")
            except Exception as e2:
                nprint(f"[flash] ERROR CRÍTICO: no se pudo reiniciar monitor: {e2}")
                svc_log.error(f"[flash] ERROR CRÍTICO: no se pudo reiniciar monitor: {e2}\r\n")


def control_server(cfg, mon: EspMonitor, svc_log: logging.Logger):
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
            nprint("[control] Petición completada")
        except Exception as e:
            nprint(f"[control] EXCEPCIÓN en control_server: {e}")
            svc_log.exception("error en control: %s", e)
            nprint(f"[control] ERROR: {e}")
            try:
                send_msg(c, {"ok": False, "error": "exception", "message": str(e)})
            except Exception:
                pass
        finally:
            try:
                c.close()
                svc_log.info(f"[control] conexión cerrada\r\n")
            except Exception:
                pass

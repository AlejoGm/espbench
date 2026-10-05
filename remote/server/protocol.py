#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
protocol.py — protocolo TCP de control para flasheo remoto de ESP32.

Una conexión = un pedido (upload_and_flash, pull_and_flash o unlock). Flujo:

    authenticate → validate_action → LockStore (unlock termina acá)
    → ACK → receive_artifact → extract_artifact
    → monitor_paused { verificar MAC → run_flash } → copiar .elf → respuesta

monitor_paused pasa por la FSM del device (start_flash/finish_flash): no se
flashea en medio de un erase, y mientras dura el proceso no se deja matar por
una señal. Los datos del flash (job, .elf, último usuario) van a
devices/<mac>/ si se conoce la MAC; si no, a las rutas por tty de siempre.

Cada paso es una función con entrada y salida propias, testeable con fakes
(ver tests/test_protocol.py). Antes todo esto era una única función de 375
líneas que solo se podía probar con socket, esptool y monitor reales.

Las herramientas externas (buscar esptool, leer MAC, armar y correr comandos)
llegan en FlashTools, así los tests corren sin hardware.
"""
import contextlib
import dataclasses
import datetime as dt
import logging
import pathlib
import shutil
import socket
import sys
import time
import zipfile
from typing import Callable, Optional
from urllib.request import Request, urlopen

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from common import recv_msg, send_msg, sha256_file
from server import paths, taglog
from server.flash import build_esptool_cmd, find_esptool_cmd, read_mac, run_cmd
from server.device import Device, DeviceState, InvalidTransition

TAG = "protocol"

CHUNK_SIZE = 1024 * 1024
CHUNK_PROGRESS_INTERVAL = 5 * CHUNK_SIZE
TCP_BACKLOG = 5
TIMEOUT_DOWNLOAD = 120  # segundos
ACTIONS = ("upload_and_flash", "pull_and_flash", "unlock")
APP_OFFSETS = ("0x10000", "0x120000")
ESPTOOL_RC_HINTS = {
    0: "OK",
    1: "Error general de esptool.",
    2: "Error fatal de conexión: boot mode incorrecto, chip no responde o puerto ocupado.",
    -1: "Error interno al lanzar esptool.",
}


def ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)


@dataclasses.dataclass
class FlashTools:
    """Todo lo que protocol necesita de afuera. En tests se reemplaza por fakes."""
    find_esptool: Callable[[], list] = find_esptool_cmd
    read_mac: Callable[[str], Optional[str]] = read_mac
    build_cmd: Callable = build_esptool_cmd
    run: Callable = run_cmd


@dataclasses.dataclass
class FlashResult:
    rc_erase: int
    rc_write: int
    pairs: list
    started_at: str
    finished_at: str

    @property
    def ok(self) -> bool:
        return self.rc_erase == 0 and self.rc_write == 0

    @property
    def missing_app(self) -> bool:
        return not any(off in APP_OFFSETS for off, _ in self.pairs)


class RequestRejected(Exception):
    """Pedido rechazado con una respuesta de error ya armada para el cliente."""

    def __init__(self, response: dict):
        super().__init__(response.get("error"))
        self.response = response


# ---------- 1. autenticación y acción ----------

def authenticate(header: dict, token: str) -> None:
    if token and header.get("token") != token:
        taglog.warn(TAG, "token inválido, rechazando conexión")
        raise RequestRejected({"ok": False, "error": "unauthorized"})


def validate_action(header: dict) -> str:
    action = header.get("action")
    if action not in ACTIONS:
        taglog.error(TAG, f"acción inválida: {action}")
        raise RequestRejected({"ok": False, "error": "bad_action"})
    return action


# ---------- 2. lock por device ----------

class LockStore:
    """Lock de uso de un device: archivo "user:token". Un usuario lo toma al
    flashear y solo él (o un unlock con el mismo par) lo suelta."""

    def __init__(self, lock_file: pathlib.Path):
        self.lock_file = lock_file

    def _read(self):
        parts = self.lock_file.read_text().strip().split(":", 1)
        return parts[0], parts[1] if len(parts) > 1 else ""

    def unlock(self, user: str, token: str) -> dict:
        if not user or not token:
            return {"ok": False, "error": "lock_credentials_required"}
        if self.lock_file.exists():
            stored_user, stored_token = self._read()
            if stored_user != user or stored_token != token:
                return {"ok": False, "error": "token_mismatch", "message": "Par user/token incorrecto"}
            self.lock_file.unlink()
        return {"ok": True, "message": "desbloqueado"}

    def acquire(self, user: str, token: str) -> None:
        if not user or not token:
            taglog.warn(TAG, "lock_user/lock_token ausente, rechazando")
            raise RequestRejected({"ok": False, "error": "lock_credentials_required",
                                   "message": "Configurá 'lock_user' y 'lock_token' en .flashcfg.json > remote"})
        if self.lock_file.exists():
            stored_user, stored_token = self._read()
            if stored_user != user:
                taglog.warn(TAG, f"device bloqueado por '{stored_user}', rechazando '{user}'")
                raise RequestRejected({"ok": False, "error": "device_locked",
                                       "message": f"Dispositivo bloqueado por '{stored_user}'"})
            if stored_token != token:
                taglog.warn(TAG, f"token incorrecto para '{user}'")
                raise RequestRejected({"ok": False, "error": "token_mismatch", "message": "Token incorrecto"})
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file.write_text(f"{user}:{token}")
        try:
            self.lock_file.chmod(0o666)
        except OSError:
            pass
        taglog.info(TAG, f"lock adquirido por '{user}'")


# ---------- 3. artefacto ----------

def _verify_sha(artifact: pathlib.Path, expected: Optional[str], what: str) -> None:
    if not expected:
        return
    actual = sha256_file(artifact).lower()
    if actual != expected.lower():
        taglog.error(TAG, f"SHA256 esperado: {expected}, obtenido: {actual}")
        raise ValueError(f"hash SHA256 {what}no coincide")
    taglog.info(TAG, "SHA256 verificado OK")


def receive_artifact(sock, header: dict, action: str, artifact: pathlib.Path,
                     opener: Callable = urlopen) -> None:
    """Upload por el mismo socket, o pull desde artifact_url. Verifica SHA256."""
    if action == "upload_and_flash":
        size = int(header.get("artifact_size") or 0)
        if size <= 0:
            raise ValueError("artifact_size inválido")
        taglog.info(TAG, f"recibiendo artefacto ({size} bytes)")
        remaining = size
        with artifact.open("wb") as f:
            while remaining > 0:
                chunk = sock.recv(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise ConnectionError("transferencia interrumpida")
                f.write(chunk)
                remaining -= len(chunk)
                if remaining % CHUNK_PROGRESS_INTERVAL == 0 or remaining < CHUNK_SIZE:
                    taglog.debug(TAG, f"progreso: {size - remaining}/{size} bytes")
        _verify_sha(artifact, header.get("artifact_sha256"), "")
    else:
        url = header.get("artifact_url")
        if not url:
            raise ValueError("falta artifact_url")
        taglog.info(TAG, f"descargando artefacto desde {url}")
        downloaded = 0
        req = Request(url, headers={"User-Agent": "remote-esp32/1.0"})
        with opener(req, timeout=TIMEOUT_DOWNLOAD) as r, artifact.open("wb") as f:
            while True:
                chunk = r.read(CHUNK_SIZE)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
        taglog.info(TAG, f"descarga completa: {downloaded} bytes")
        _verify_sha(artifact, header.get("artifact_sha256"), "(pull) ")


def extract_artifact(artifact: pathlib.Path, jobdir: pathlib.Path) -> None:
    with zipfile.ZipFile(artifact, "r") as z:
        taglog.debug(TAG, f"archivos en ZIP: {z.namelist()}")
        z.extractall(jobdir)
    if not (jobdir / "flasher_args.json").exists():
        taglog.error(TAG, f"flasher_args.json no encontrado en {jobdir}")
        raise FileNotFoundError("flasher_args.json no encontrado")


def flash_params(header: dict, cfg: dict) -> dict:
    return {
        "chip": header.get("chip") or cfg["chip"],
        "baud": int(header.get("baud") or cfg["flash_baud"]),
        "encrypt": bool(header.get("encrypt", True)),
        "erase": bool(header.get("erase", False)),
    }


# ---------- 4. flash ----------

@contextlib.contextmanager
def monitor_paused(mon, device: Device):
    """FLASHING en la FSM + puerto serie libre para esptool. El monitor se
    relanza y la FSM vuelve al estado previo siempre, aunque el flash explote."""
    device.start_flash()
    try:
        mon.stop()
        yield
    finally:
        _restart_monitor(mon)
        device.finish_flash()


def _restart_monitor(mon) -> None:
    for attempt in (1, 2):
        try:
            mon.start()
            taglog.info(TAG, "monitor reiniciado")
            return
        except Exception as e:
            taglog.error(TAG, f"no se pudo reiniciar el monitor (intento {attempt}): {e}")
            if attempt == 1:
                time.sleep(1)


def run_flash(tools: FlashTools, esptool: list, tty: str, params: dict,
              jobdir: pathlib.Path, job_log=None, on_line=None) -> FlashResult:
    """erase (opcional) + write. Si write falla con rc=2 y había --encrypt,
    reintenta sin --encrypt (chip sin flash encryption habilitado)."""
    erase_cmd, write_cmd, pairs = tools.build_cmd(
        esptool, params["chip"], tty, params["baud"], params["encrypt"], params["erase"], jobdir)
    t0 = dt.datetime.now().isoformat()

    def emit(line):
        if on_line:
            on_line(line)

    emit(f"[espbench] artifact OK — {len(pairs)} archivos a flashear")
    for off, path in pairs:
        emit(f"[espbench]   {off}: {pathlib.Path(path).name}")

    rc_erase = 0
    if erase_cmd:
        emit("[espbench] erase_flash...")
        rc_erase = _safe_run(tools, erase_cmd, job_log, on_line, "erase_flash")

    emit("[espbench] write_flash...")
    rc_write = _safe_run(tools, write_cmd, job_log, on_line, "write_flash")
    if rc_write == 2 and params["encrypt"]:
        taglog.warn(TAG, "write_flash falló con código 2, reintentando sin --encrypt")
        emit("[espbench] reintentando sin --encrypt...")
        rc_write = _safe_run(tools, [c for c in write_cmd if c != "--encrypt"],
                             job_log, on_line, "write_flash sin --encrypt")

    return FlashResult(rc_erase, rc_write, pairs, t0, dt.datetime.now().isoformat())


def _safe_run(tools, cmd, job_log, on_line, what) -> int:
    try:
        rc = tools.run(cmd, job_log, on_line=on_line)
        taglog.info(TAG, f"{what} terminado con código {rc}")
        return rc
    except Exception as e:
        taglog.error(TAG, f"{what}: {e}")
        return -1


def flash_response(result: FlashResult, job_id: str, tty: str, params: dict, job_log_path) -> dict:
    if result.ok:
        status = "parcial (sin aplicación)" if result.missing_app else "exitoso"
    else:
        status = "fallido"
    resp = {
        "ok": result.ok, "job_id": job_id, "started_at": result.started_at,
        "finished_at": result.finished_at, "device": tty, "chip": params["chip"],
        "baud": params["baud"], "erase_rc": result.rc_erase, "write_rc": result.rc_write,
        "pairs": result.pairs, "log_file": str(job_log_path), "status": status,
        "missing_app": result.missing_app,
    }
    if not result.ok:
        failing = result.rc_write if result.rc_write != 0 else result.rc_erase
        resp["error_hint"] = ESPTOOL_RC_HINTS.get(failing, f"exit code {failing} desconocido.")
    return resp


@contextlib.contextmanager
def _job_logger(job_id: str, path: pathlib.Path):
    log = logging.getLogger(f"job.{job_id}")
    log.setLevel(logging.INFO)
    log.propagate = False
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    try:
        yield log
    finally:
        log.removeHandler(handler)
        handler.close()


# ---------- rutas y estado del device ----------

def check_flashable(device: Device) -> None:
    """Rechazar antes del ACK (y antes de recibir el artefacto) si el device no
    puede flashearse ahora: por ejemplo, un erase interactivo en curso."""
    if device.state not in (DeviceState.MONITORING, DeviceState.UNKNOWN):
        taglog.warn(TAG, f"pedido rechazado: device en {device.state.value}")
        raise RequestRejected({"ok": False, "error": "device_busy",
                               "message": f"Device ocupado ({device.state.value})"})


def job_dir(device: Device, job_id: str) -> pathlib.Path:
    """devices/<mac>/jobs/<job_id> si se conoce la MAC (el historial sigue a la
    placa aunque cambie de puerto). Si no, jobs/<job_id>_<tty>: el tty en el
    nombre es lo que usa DeviceRegistry para atribuir el último flasheo."""
    if device.mac:
        return paths.device_jobs_dir(device.mac) / job_id
    return paths.jobs_dir() / f"{job_id}_{device.tty_name}"


def current_elf(device: Device) -> pathlib.Path:
    if device.mac:
        return paths.device_current_elf(device.mac)
    return paths.current_elf_file(device.tty_name)


# ---------- orquestador ----------

def handle_control(sock, cfg: dict, mon, device: Device, tools: Optional[FlashTools] = None) -> None:
    tools = tools or FlashTools()
    tty = cfg["tty"]

    try:
        header = recv_msg(sock)
        authenticate(header, str(cfg.get("token") or ""))
        action = validate_action(header)
        locks = LockStore(paths.lock_file(device.tty_name))
        user = header.get("lock_user", "").strip()
        token = header.get("lock_token", "").strip()
        if action == "unlock":
            send_msg(sock, locks.unlock(user, token))
            return
        check_flashable(device)
        locks.acquire(user, token)
    except RequestRejected as r:
        send_msg(sock, r.response)
        return

    stream = bool(header.get("stream", False))

    def stream_line(line: str):
        if not stream:
            return
        try:
            send_msg(sock, {"phase": "log", "line": line})
        except Exception:
            pass

    job_id = header.get("job_id") or time.strftime("job_%Y%m%d_%H%M%S")
    jobdir = job_dir(device, job_id)
    ensure_dir(jobdir)
    taglog.info(TAG, f"job {job_id} ({action}) por '{user}'")
    send_msg(sock, {"ok": True, "phase": "ready", "job_id": job_id})

    artifact = jobdir / "artifact.zip"
    receive_artifact(sock, header, action, artifact)   # errores -> control_server responde "exception"
    extract_artifact(artifact, jobdir)
    params = flash_params(header, cfg)
    taglog.info(TAG, f"parámetros: chip={params['chip']} baud={params['baud']} "
                     f"encrypt={params['encrypt']} erase={params['erase']}")

    job_log_path = jobdir / "job.log"

    try:
        with monitor_paused(mon, device), _job_logger(job_id, job_log_path) as job_log:
            try:
                esptool = tools.find_esptool()
            except Exception as e:
                taglog.error(TAG, f"esptool no encontrado: {e}")
                send_msg(sock, {"ok": False, "error": "esptool_not_found", "message": str(e)})
                return

            mismatch = _device_changed(tools, tty, device)
            if mismatch:
                send_msg(sock, mismatch)
                return

            try:
                result = run_flash(tools, esptool, tty, params, jobdir, job_log, on_line=stream_line)
            except Exception as e:
                taglog.error(TAG, f"no se pudieron armar los comandos de flasheo: {e}")
                send_msg(sock, {"ok": False, "error": "build_cmd_failed", "message": str(e)})
                return

            resp = flash_response(result, job_id, tty, params, job_log_path)
            taglog.info(TAG, f"resultado: {resp['status']} (erase={result.rc_erase}, write={result.rc_write})")
            if result.ok:
                _after_success(jobdir, device, user)
            send_msg(sock, {**resp, "phase": "done"})
    except Exception as e:
        taglog.error(TAG, f"error crítico durante el flash: {e}")
        try:
            send_msg(sock, {"ok": False, "error": "flash_critical_error", "message": str(e)})
        except Exception:
            pass


def _device_changed(tools: FlashTools, tty: str, device: Device) -> Optional[dict]:
    """El device del puerto tiene que ser el mismo que se identificó al arrancar
    la sesión. Si se cambió de placa sin reiniciar la sesión, no flashear.
    Si la sesión arrancó sin MAC (UNKNOWN) y ahora se puede leer, se adopta."""
    mac_now = tools.read_mac(tty)
    if not mac_now:
        taglog.warn(TAG, "no se pudo leer la MAC antes de flashear (continuando)")
        return None
    if device.mac is None:
        try:
            device.promote(mac_now)
        except InvalidTransition:
            pass
    elif mac_now.upper() != device.mac.upper():
        taglog.error(TAG, f"MAC cambió: registrada={device.mac}, actual={mac_now}")
        return {"ok": False, "error": "device_changed",
                "message": f"Dispositivo cambiado (MAC esperada: {device.mac}). Reiniciar sesión."}
    taglog.info(TAG, f"MAC verificada: {mac_now}")
    return None


def _after_success(jobdir: pathlib.Path, device: Device, user: str) -> None:
    elf = jobdir / "firmware.elf"
    if elf.exists():
        dst = current_elf(device)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(elf, dst)
        taglog.info(TAG, f"firmware.elf → {dst}")
    try:
        f = (paths.device_last_user(device.mac) if device.mac
             else paths.last_user_file(device.tty_name))
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(user)
        f.chmod(0o666)
    except OSError:
        pass


def serve_connection(conn, cfg: dict, mon, device: Device, tools: Optional[FlashTools] = None) -> None:
    """Atiende una conexión y la cierra. Cualquier error no previsto (SHA256,
    ZIP inválido, transferencia cortada) le llega al cliente como "exception"."""
    try:
        handle_control(conn, cfg, mon, device, tools)
    except Exception as e:
        taglog.error(TAG, f"error atendiendo pedido: {e}")
        try:
            send_msg(conn, {"ok": False, "error": "exception", "message": str(e)})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def control_server(cfg: dict, mon, device: Device, tools: Optional[FlashTools] = None) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", cfg["port"]))
    srv.listen(TCP_BACKLOG)
    taglog.info(TAG, f"escuchando en 0.0.0.0:{cfg['port']} (tty={cfg['tty']})")
    while True:
        conn, addr = srv.accept()
        taglog.info(TAG, f"conexión desde {addr[0]}:{addr[1]}")
        serve_connection(conn, cfg, mon, device, tools)

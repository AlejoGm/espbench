import dataclasses
import fcntl
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from common import mac_to_sn_sfy, hw_model_from_project_name
from server import history, locks, paths, runstate


class DevicesFile:
    """Process-safe read/write de devices.json (fcntl.flock). Ver paths.devices_file()."""

    def __init__(self, path: Optional[pathlib.Path] = None):
        self._path = path or paths.devices_file()
        self._lock = threading.Lock()

    def _update(self, updater, silent: bool = True):
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.touch(mode=0o666, exist_ok=True)
                with open(self._path, "r+") as f:
                    fcntl.flock(f, fcntl.LOCK_EX)
                    try:
                        content = f.read()
                        data = json.loads(content) if content.strip() else {}
                        updater(data)
                        f.seek(0)
                        f.truncate()
                        f.write(json.dumps(data, indent=2))
                        # El flush tiene que pasar ANTES de soltar el flock: f.write()
                        # solo llega al buffer de Python, y el cierre del `with` (que
                        # es lo que flushea) ocurre despues del LOCK_UN. Sin esto, dos
                        # procesos que registran MAC a la vez (ej. devremote --reset,
                        # que levanta todos los devices juntos) intercalan truncate y
                        # flush, y el archivo queda con un JSON corto + la cola del
                        # anterior -> "Extra data" al parsear.
                        f.flush()
                        os.fsync(f.fileno())
                    finally:
                        fcntl.flock(f, fcntl.LOCK_UN)
                try:
                    self._path.chmod(0o666)
                except Exception:
                    pass
            except Exception:
                if not silent:
                    raise

    def register_mac(self, mac: str, sn: str):
        """Create entry for MAC if not present. device_key defaults to SN."""
        def _do(data):
            key = mac.upper()
            if key not in data:
                data[key] = {"device_key": sn, "hw_model": None}
        self._update(_do)

    def update_hw_model(self, mac: str, hw_model: str):
        def _do(data):
            entry = data.get(mac.upper())
            if entry is not None:
                entry["hw_model"] = hw_model
        self._update(_do)

    def update_device_key(self, mac: str, device_key: str):
        def _do(data):
            mac_up = mac.upper()
            if mac_up not in data:
                data[mac_up] = {"device_key": device_key, "hw_model": None}
            else:
                data[mac_up]["device_key"] = device_key
        self._update(_do, silent=False)

    def get_all(self) -> dict:
        with self._lock:
            if not self._path.exists():
                return {}
            try:
                # Lock compartido: sin esto se puede leer un archivo a medio
                # reescribir por otro proceso.
                with open(self._path, "r") as f:
                    fcntl.flock(f, fcntl.LOCK_SH)
                    try:
                        content = f.read()
                    finally:
                        fcntl.flock(f, fcntl.LOCK_UN)
                return json.loads(content) if content.strip() else {}
            except Exception as e:
                # Devolver {} es el fallback, pero en silencio esconde un
                # devices.json corrupto: el dashboard lista los devices sin sus
                # nombres y nadie se entera hasta que falla un rename.
                print(f"[device_registry] devices.json ilegible: {e}", flush=True)
                return {}

    def resolve_board(self, key: str) -> Optional[str]:
        """device_key, SN o MAC (con o sin separadores) → MAC como está en
        devices.json. Una MAC que no está en devices.json pero tiene
        devices/<MAC>/ también vale. None si no hay placa."""
        key = (key or "").strip()
        if not key:
            return None
        data = self.get_all()
        bare = key.upper().replace(":", "").replace("-", "")
        if re.fullmatch(r"[0-9A-F]{12}", bare):
            for mac in data:
                if mac.upper().replace(":", "") == bare:
                    return mac
            if paths.device_home(bare).is_dir():
                return ":".join(bare[i:i + 2] for i in range(0, 12, 2))
        for mac, entry in data.items():
            if entry.get("device_key") == key:
                return mac
        for mac in data:
            try:
                if mac_to_sn_sfy(mac).upper() == key.upper():
                    return mac
            except Exception:
                continue
        return None

    def find_by_key(self, device_key: str) -> Optional[tuple]:
        """Return (mac, entry) for the given device_key, or None."""
        for mac, entry in self.get_all().items():
            if entry.get("device_key") == device_key:
                return mac, entry
        return None


@dataclasses.dataclass
class DeviceInfo:
    tty: str
    tty_name: str
    port_tcp: int
    status: str
    last_flash_ts: Optional[str]
    last_flash_user: Optional[str]
    mac: Optional[str]
    sn: Optional[str]
    device_key: Optional[str]
    hw_model: Optional[str]
    fw_project: Optional[str]
    fw_version: Optional[str]
    fw_idf: Optional[str]
    lock_user: Optional[str]
    # Estado de la FSM del proceso del device (discovering, monitoring, flashing,
    # erasing, unknown, disconnected). None si no hay proceso vivo que lo publique.
    state: Optional[str] = None
    # Lo que SerialWatch publica en run/<tty>.json: reinicios, panics, boot loop.
    health: Optional[dict] = None
    # Cómo terminó el último flasheo (result.json). None si no hay o es anterior a result.json.
    last_flash_ok: Optional[bool] = None
    # Vencimiento del lock si es una reserva (ISO con el offset de la Pi, y en
    # epoch); None si es el lock permanente del flash o no hay lock. Un lock
    # vencido no aparece.
    lock_expires: Optional[str] = None
    lock_expires_epoch: Optional[int] = None
    # Última escritura del log de la sesión (epoch): el dashboard marca "sin log"
    # una placa que monitorea pero no imprime nada hace rato.
    last_log_epoch: Optional[float] = None


class DeviceRegistry:
    def __init__(self, dev_dir: str = "/dev", jobs_dir: Optional[str] = None,
                 devices_file: Optional[DevicesFile] = None):
        self._dev_dir = pathlib.Path(dev_dir)
        self._jobs_dir = pathlib.Path(jobs_dir) if jobs_dir else paths.jobs_dir()
        self._devices_file = devices_file or DevicesFile()

    def list_devices(self) -> list[DeviceInfo]:
        return [self._build_device_info(name) for name in self._device_names()]

    def _device_names(self) -> list:
        """Un device por puerto físico: /dev/esp-slotK si su puerto está mapeado
        en slots.conf (symlink estable que crea udev), si no /dev/ttyUSBN. El
        ttyUSBN al que apunta un slot no se lista dos veces."""
        slots = list(self._dev_dir.glob("esp-slot*"))
        claimed = {os.path.realpath(s) for s in slots}
        ttys = [t for t in self._dev_dir.glob("ttyUSB*") if os.path.realpath(t) not in claimed]
        return sorted((p.name for p in slots + ttys), key=_natural_key)

    def get_device(self, tty_name: str) -> Optional[DeviceInfo]:
        if not (self._dev_dir / tty_name).exists():
            return None
        return self._build_device_info(tty_name)

    def get_device_by_key(self, device_key: str) -> Optional[DeviceInfo]:
        result = self._devices_file.find_by_key(device_key)
        if result is None:
            return None
        mac, _ = result
        for tty_name in self._device_names():
            tty_mac = self._get_tty_mac(tty_name)
            if tty_mac and tty_mac.upper() == mac.upper():
                return self._build_device_info(tty_name)
        return None

    def update_device_key(self, mac: str, device_key: str):
        self._devices_file.update_device_key(mac, device_key)

    @staticmethod
    def _get_tty_mac(tty_name: str, state: Optional[dict] = None) -> Optional[str]:
        """La MAC la publica el proceso del device en run/<tty>.json. El archivo
        logs/<tty>/mac es del esquema anterior (sesiones viejas todavía vivas)."""
        state = state if state is not None else runstate.read(tty_name)
        if state and state.get("mac"):
            return state["mac"]
        f = paths.mac_file(tty_name)
        try:
            if f.exists():
                return f.read_text().strip() or None
        except Exception:
            pass
        return None

    def _build_device_info(self, tty_name: str) -> DeviceInfo:
        state = runstate.read(tty_name) or {}
        # El puerto lo decide la capa de infra y llega por --control-port; el
        # proceso lo publica. Derivarlo del nombre es solo el fallback.
        port_tcp = state.get("tcp_port") or 5000 + self._parse_tty_number(tty_name)
        # El firmware lo publica el proceso del device (SerialWatch), que ve todo el serial.
        fw = {f"fw_{key}": value for key, value in (state.get("fw") or {}).items() if value}
        mac = self._get_tty_mac(tty_name, state)
        sn = device_key = hw_model = None
        if mac:
            try:
                sn = mac_to_sn_sfy(mac)
            except Exception:
                pass
            entry = self._devices_file.get_all().get(mac.upper(), {})
            device_key = entry.get("device_key")
            hw_model   = entry.get("hw_model")
            if hw_model is None and fw.get("fw_project"):
                hw_model = hw_model_from_project_name(fw["fw_project"])
                self._devices_file.update_hw_model(mac, hw_model)
        last_flash_ts = self._get_last_flash_ts(tty_name, mac)
        latest = history.list_jobs(tty_name, mac, limit=1)
        last_flash_ok = latest[0]["ok"] if latest and latest[0]["ts"] == last_flash_ts else None
        lock = locks.read(tty_name)
        return DeviceInfo(
            tty=str(self._dev_dir / tty_name),
            tty_name=tty_name,
            port_tcp=port_tcp,
            status=self._get_status(tty_name, state),
            last_flash_ts=last_flash_ts,
            last_flash_user=self._get_last_flash_user(tty_name, mac),
            mac=mac,
            sn=sn,
            device_key=device_key,
            hw_model=hw_model,
            fw_project=fw.get("fw_project"),
            fw_version=fw.get("fw_version"),
            fw_idf=fw.get("fw_idf"),
            lock_user=lock.user if lock else None,
            state=self._live_state(state),
            health=state.get("health"),
            last_flash_ok=last_flash_ok,
            lock_expires=lock.expires_iso_tz() if lock else None,
            lock_expires_epoch=lock.expires if lock else None,
            last_log_epoch=self._log_mtime(state),
        )

    @staticmethod
    def _log_mtime(state: dict) -> Optional[float]:
        try:
            return os.stat(state["log_path"]).st_mtime if state.get("log_path") else None
        except OSError:
            return None

    @staticmethod
    def _get_last_flash_user(tty_name: str, mac: Optional[str] = None) -> Optional[str]:
        files = ([paths.device_last_user(mac)] if mac else []) + [paths.last_user_file(tty_name)]
        for f in files:
            try:
                if f.exists():
                    return f.read_text().strip() or None
            except Exception:
                pass
        return None

    @staticmethod
    def _parse_tty_number(tty_name: str) -> int:
        """Fallback para sesiones viejas que no publican tcp_port: ttyUSBN / esp-slotK → N/K."""
        m = re.search(r"(\d+)$", tty_name)
        return int(m.group(1)) if m else 0

    @staticmethod
    def _live_state(state: dict) -> Optional[str]:
        if not state:
            return None
        if state.get("state") == "disconnected":
            return "disconnected"
        return state.get("state") if runstate.pid_alive(state.get("pid")) else None

    def _get_status(self, tty_name: str, state: Optional[dict] = None) -> str:
        """RUNNING si el proceso del device publica estado y sigue vivo. Sin
        estado runtime (sesión con código anterior) se infiere por tmux."""
        if state:
            live = self._live_state(state)
            return "RUNNING" if live and live != "disconnected" else "DOWN"
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", f"esp32_{tty_name}"],
                capture_output=True,
            )
            return "RUNNING" if result.returncode == 0 else "DOWN"
        except FileNotFoundError:
            return "DOWN"

    def _get_last_flash_ts(self, tty_name: str, mac: Optional[str] = None) -> Optional[str]:
        """Más reciente entre devices/<mac>/jobs/ (si se conoce la MAC) y los jobs
        por tty del esquema anterior (jobs/job_*_<tty>). Sin fallback a
        "cualquier job": devolvía el flasheo de otro device."""
        job_dirs = []
        if mac:
            mac_jobs = paths.device_jobs_dir(mac)
            if mac_jobs.is_dir():
                job_dirs += list(mac_jobs.glob("job_*"))
        if self._jobs_dir.exists():
            job_dirs += list(self._jobs_dir.glob(f"job_*_{tty_name}"))
        stamps = [ts for ts in (self._parse_job_timestamp(d.name) for d in job_dirs) if ts]
        return max(stamps) if stamps else None

    @staticmethod
    def _parse_job_timestamp(dirname: str) -> Optional[str]:
        prefix = "job_"
        if not dirname.startswith(prefix):
            return None
        parts = dirname[len(prefix):].split("_")
        if len(parts) < 2:
            return None
        date_part, time_part = parts[0], parts[1]
        if len(date_part) != 8 or len(time_part) != 6:
            return None
        y, mo, d = date_part[:4], date_part[4:6], date_part[6:]
        h, mi, s = time_part[:2], time_part[2:4], time_part[4:]
        return f"{y}-{mo}-{d}T{h}:{mi}:{s}"


def _natural_key(name: str):
    """esp-slot2 < esp-slot10 < ttyUSB0 < ttyUSB10 (no orden alfabético)."""
    m = re.match(r"(\D*)(\d*)$", name)
    return (m.group(1), int(m.group(2)) if m.group(2) else -1)

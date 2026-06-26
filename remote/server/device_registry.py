import dataclasses
import fcntl
import json
import pathlib
import subprocess
import sys
import threading
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from common import mac_to_sn_sfy, hw_model_from_project_name

_LOCKS_DIR   = pathlib.Path("/opt/esp/locks")
_LOGS_DIR    = pathlib.Path("/opt/esp/logs")
_DEVICES_FILE = pathlib.Path("/opt/esp/devices.json")


class DevicesFile:
    """Process-safe read/write of /opt/esp/devices.json (fcntl.flock)."""

    def __init__(self, path: pathlib.Path = _DEVICES_FILE):
        self._path = path
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
                    finally:
                        fcntl.flock(f, fcntl.LOCK_UN)
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
                return json.loads(self._path.read_text())
            except Exception:
                return {}

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


class DeviceRegistry:
    def __init__(self, dev_dir: str = "/dev", jobs_dir: str = "/opt/esp/jobs",
                 devices_file: Optional[DevicesFile] = None):
        self._dev_dir = pathlib.Path(dev_dir)
        self._jobs_dir = pathlib.Path(jobs_dir)
        self._fw_info: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._devices_file = devices_file or DevicesFile()

    def list_devices(self) -> list[DeviceInfo]:
        return [
            self._build_device_info(e.name)
            for e in sorted(self._dev_dir.glob("ttyUSB*"))
        ]

    def get_device(self, tty_name: str) -> Optional[DeviceInfo]:
        if not (self._dev_dir / tty_name).exists():
            return None
        return self._build_device_info(tty_name)

    def get_device_by_key(self, device_key: str) -> Optional[DeviceInfo]:
        result = self._devices_file.find_by_key(device_key)
        if result is None:
            return None
        mac, _ = result
        for entry in sorted(self._dev_dir.glob("ttyUSB*")):
            tty_name = entry.name
            tty_mac = self._get_tty_mac(tty_name)
            if tty_mac and tty_mac.upper() == mac.upper():
                return self._build_device_info(tty_name)
        return None

    def set_chip_id(self, tty_name: str, chip_id: str) -> None:
        pass  # SN now derived from MAC; kept for log_streamer compat

    def set_firmware_info(self, tty_name: str, project: str = None,
                          version: str = None, idf: str = None) -> None:
        with self._lock:
            info = self._fw_info.setdefault(tty_name, {})
            if project:
                info["fw_project"] = project
                hw_model = hw_model_from_project_name(project)
                mac = self._get_tty_mac(tty_name)
                if mac:
                    self._devices_file.update_hw_model(mac, hw_model)
            if version:
                info["fw_version"] = version
            if idf:
                info["fw_idf"] = idf

    def update_device_key(self, mac: str, device_key: str):
        self._devices_file.update_device_key(mac, device_key)

    @staticmethod
    def _get_tty_mac(tty_name: str) -> Optional[str]:
        f = _LOGS_DIR / tty_name / "mac"
        try:
            if f.exists():
                return f.read_text().strip() or None
        except Exception:
            pass
        return None

    def _build_device_info(self, tty_name: str) -> DeviceInfo:
        number   = self._parse_tty_number(tty_name)
        port_tcp = 5000 + number
        with self._lock:
            fw = self._fw_info.get(tty_name, {})
        mac = self._get_tty_mac(tty_name)
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
        return DeviceInfo(
            tty=str(self._dev_dir / tty_name),
            tty_name=tty_name,
            port_tcp=port_tcp,
            status=self._get_status(tty_name),
            last_flash_ts=self._get_last_flash_ts(tty_name),
            last_flash_user=self._get_last_flash_user(tty_name),
            mac=mac,
            sn=sn,
            device_key=device_key,
            hw_model=hw_model,
            fw_project=fw.get("fw_project"),
            fw_version=fw.get("fw_version"),
            fw_idf=fw.get("fw_idf"),
            lock_user=self._get_lock_user(tty_name),
        )

    @staticmethod
    def _get_last_flash_user(tty_name: str) -> Optional[str]:
        try:
            f = _LOGS_DIR / tty_name / "last_user"
            if f.exists():
                return f.read_text().strip() or None
        except Exception:
            pass
        return None

    @staticmethod
    def _get_lock_user(tty_name: str) -> Optional[str]:
        try:
            f = _LOCKS_DIR / tty_name
            if f.exists():
                content = f.read_text().strip()
                return content.split(":", 1)[0] or None
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_tty_number(tty_name: str) -> int:
        suffix = tty_name.replace("ttyUSB", "")
        try:
            return int(suffix)
        except ValueError:
            return 0

    def _get_status(self, tty_name: str) -> str:
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", f"esp32_{tty_name}"],
                capture_output=True,
            )
            return "RUNNING" if result.returncode == 0 else "DOWN"
        except FileNotFoundError:
            return "DOWN"

    def _get_last_flash_ts(self, tty_name: str) -> Optional[str]:
        if not self._jobs_dir.exists():
            return None
        job_dirs = sorted(self._jobs_dir.glob(f"job_*_{tty_name}"), reverse=True)
        if not job_dirs:
            job_dirs = sorted(self._jobs_dir.glob("job_*"), reverse=True)
        for job_dir in job_dirs:
            ts = self._parse_job_timestamp(job_dir.name)
            if ts is not None:
                return ts
        return None

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

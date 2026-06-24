import dataclasses
import pathlib
import subprocess
import threading
from typing import Optional


@dataclasses.dataclass
class DeviceInfo:
    tty: str
    tty_name: str
    port_tcp: int
    status: str
    last_flash_ts: Optional[str]
    chip_id: Optional[str]
    fw_project: Optional[str]
    fw_version: Optional[str]
    fw_idf: Optional[str]


class DeviceRegistry:
    def __init__(self, dev_dir: str = "/dev", jobs_dir: str = "/opt/esp/jobs"):
        self._dev_dir = pathlib.Path(dev_dir)
        self._jobs_dir = pathlib.Path(jobs_dir)
        self._chip_ids: dict[str, str] = {}
        self._fw_info: dict[str, dict] = {}
        self._lock = threading.Lock()

    def list_devices(self) -> list[DeviceInfo]:
        devices = []
        for entry in sorted(self._dev_dir.glob("ttyUSB*")):
            tty_name = entry.name
            devices.append(self._build_device_info(tty_name))
        return devices

    def get_device(self, tty_name: str) -> Optional[DeviceInfo]:
        path = self._dev_dir / tty_name
        if not path.exists():
            return None
        return self._build_device_info(tty_name)

    def set_chip_id(self, tty_name: str, chip_id: str) -> None:
        with self._lock:
            self._chip_ids[tty_name] = chip_id

    def set_firmware_info(self, tty_name: str, project: str = None, version: str = None, idf: str = None) -> None:
        with self._lock:
            info = self._fw_info.setdefault(tty_name, {})
            if project: info['fw_project'] = project
            if version: info['fw_version'] = version
            if idf:     info['fw_idf'] = idf

    def _build_device_info(self, tty_name: str) -> DeviceInfo:
        tty = str(self._dev_dir / tty_name)
        number = self._parse_tty_number(tty_name)
        port_tcp = 5000 + number
        status = self._get_status(tty_name)
        last_flash_ts = self._get_last_flash_ts(tty_name)
        with self._lock:
            chip_id = self._chip_ids.get(tty_name)
            fw = self._fw_info.get(tty_name, {})
        return DeviceInfo(
            tty=tty,
            tty_name=tty_name,
            port_tcp=port_tcp,
            status=status,
            last_flash_ts=last_flash_ts,
            chip_id=chip_id,
            fw_project=fw.get('fw_project'),
            fw_version=fw.get('fw_version'),
            fw_idf=fw.get('fw_idf'),
        )

    @staticmethod
    def _parse_tty_number(tty_name: str) -> int:
        # ttyUSB0 → 0, ttyUSB3 → 3
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
        job_dirs = sorted(self._jobs_dir.glob("job_*"), reverse=True)
        for job_dir in job_dirs:
            ts = self._parse_job_timestamp(job_dir.name)
            if ts is not None:
                return ts
        return None

    @staticmethod
    def _parse_job_timestamp(dirname: str) -> Optional[str]:
        # dirname format: job_YYYYMMDD_HHMMSS
        prefix = "job_"
        if not dirname.startswith(prefix):
            return None
        rest = dirname[len(prefix):]
        parts = rest.split("_")
        if len(parts) != 2:
            return None
        date_part, time_part = parts
        if len(date_part) != 8 or len(time_part) != 6:
            return None
        year = date_part[0:4]
        month = date_part[4:6]
        day = date_part[6:8]
        hour = time_part[0:2]
        minute = time_part[2:4]
        second = time_part[4:6]
        return f"{year}-{month}-{day}T{hour}:{minute}:{second}"

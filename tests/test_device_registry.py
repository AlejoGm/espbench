import pathlib
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))
from server.device_registry import DeviceRegistry, DeviceInfo


def make_registry(tmp_path: pathlib.Path, create_jobs_dir: bool = True):
    dev_dir = tmp_path / "dev"
    dev_dir.mkdir()
    jobs_dir = tmp_path / "jobs"
    if create_jobs_dir:
        jobs_dir.mkdir()
    return DeviceRegistry(dev_dir=str(dev_dir), jobs_dir=str(jobs_dir)), dev_dir, jobs_dir


def mock_tmux_down(*args, **kwargs):
    result = MagicMock()
    result.returncode = 1
    return result


def mock_tmux_up(*args, **kwargs):
    result = MagicMock()
    result.returncode = 0
    return result


class TestListDevices:
    def test_list_devices_empty(self, tmp_path):
        registry, dev_dir, _ = make_registry(tmp_path)
        with patch("subprocess.run", side_effect=mock_tmux_down):
            devices = registry.list_devices()
        assert devices == []

    def test_list_devices_finds_devices(self, tmp_path):
        registry, dev_dir, _ = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        (dev_dir / "ttyUSB1").touch()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            devices = registry.list_devices()
        assert len(devices) == 2
        names = {d.tty_name for d in devices}
        assert names == {"ttyUSB0", "ttyUSB1"}

    def test_list_devices_ignores_non_ttyusb(self, tmp_path):
        registry, dev_dir, _ = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        (dev_dir / "ttyS0").touch()
        (dev_dir / "null").touch()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            devices = registry.list_devices()
        assert len(devices) == 1
        assert devices[0].tty_name == "ttyUSB0"


class TestPortCalculation:
    def test_port_calculation_usb0(self, tmp_path):
        registry, dev_dir, _ = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            devices = registry.list_devices()
        assert devices[0].port_tcp == 5000

    def test_port_calculation_usb3(self, tmp_path):
        registry, dev_dir, _ = make_registry(tmp_path)
        (dev_dir / "ttyUSB3").touch()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            devices = registry.list_devices()
        assert devices[0].port_tcp == 5003


class TestStatus:
    def test_status_running_when_tmux_rc0(self, tmp_path):
        registry, dev_dir, _ = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        with patch("subprocess.run", side_effect=mock_tmux_up):
            devices = registry.list_devices()
        assert devices[0].status == "RUNNING"

    def test_status_down_when_tmux_rc1(self, tmp_path):
        registry, dev_dir, _ = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            devices = registry.list_devices()
        assert devices[0].status == "DOWN"

    def test_status_down_when_tmux_not_found(self, tmp_path):
        registry, dev_dir, _ = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            devices = registry.list_devices()
        assert devices[0].status == "DOWN"


class TestChipId:
    def test_chip_id_none_by_default(self, tmp_path):
        registry, dev_dir, _ = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            devices = registry.list_devices()
        assert devices[0].chip_id is None

    def test_set_and_get_chip_id(self, tmp_path):
        registry, dev_dir, _ = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        registry.set_chip_id("ttyUSB0", "123456")
        with patch("subprocess.run", side_effect=mock_tmux_down):
            device = registry.get_device("ttyUSB0")
        assert device is not None
        assert device.chip_id == "123456"

    def test_set_chip_id_does_not_affect_other_device(self, tmp_path):
        registry, dev_dir, _ = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        (dev_dir / "ttyUSB1").touch()
        registry.set_chip_id("ttyUSB0", "AABBCC")
        with patch("subprocess.run", side_effect=mock_tmux_down):
            d1 = registry.get_device("ttyUSB1")
        assert d1.chip_id is None


class TestLastFlashTs:
    def test_last_flash_ts_none_when_no_jobs(self, tmp_path):
        registry, dev_dir, jobs_dir = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            device = registry.get_device("ttyUSB0")
        assert device.last_flash_ts is None

    def test_last_flash_ts_none_when_jobs_dir_missing(self, tmp_path):
        registry, dev_dir, jobs_dir = make_registry(tmp_path, create_jobs_dir=False)
        (dev_dir / "ttyUSB0").touch()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            device = registry.get_device("ttyUSB0")
        assert device.last_flash_ts is None

    def test_last_flash_ts_from_jobs(self, tmp_path):
        registry, dev_dir, jobs_dir = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        (jobs_dir / "job_20260623_120000").mkdir()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            device = registry.get_device("ttyUSB0")
        assert device.last_flash_ts == "2026-06-23T12:00:00"

    def test_last_flash_ts_picks_most_recent(self, tmp_path):
        registry, dev_dir, jobs_dir = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        (jobs_dir / "job_20260623_100000").mkdir()
        (jobs_dir / "job_20260623_120000").mkdir()
        (jobs_dir / "job_20260622_235900").mkdir()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            device = registry.get_device("ttyUSB0")
        assert device.last_flash_ts == "2026-06-23T12:00:00"

    def test_last_flash_ts_ignores_non_job_dirs(self, tmp_path):
        registry, dev_dir, jobs_dir = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        (jobs_dir / "other_dir").mkdir()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            device = registry.get_device("ttyUSB0")
        assert device.last_flash_ts is None


class TestGetDevice:
    def test_get_device_returns_none_for_missing(self, tmp_path):
        registry, dev_dir, _ = make_registry(tmp_path)
        with patch("subprocess.run", side_effect=mock_tmux_down):
            device = registry.get_device("ttyUSB99")
        assert device is None

    def test_get_device_returns_device_info(self, tmp_path):
        registry, dev_dir, _ = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            device = registry.get_device("ttyUSB0")
        assert isinstance(device, DeviceInfo)
        assert device.tty_name == "ttyUSB0"
        assert device.port_tcp == 5000

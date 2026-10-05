import fcntl
import json
import pathlib
import sys
from unittest.mock import patch, MagicMock

import pytest

_repo_root = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "remote"))
from server.device_registry import DeviceRegistry, DeviceInfo, DevicesFile


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


class TestSn:
    def test_sn_none_without_mac_file(self, tmp_path):
        registry, dev_dir, _ = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            devices = registry.list_devices()
        assert devices[0].sn is None
        assert devices[0].mac is None
        assert devices[0].device_key is None

    def test_sn_derived_from_mac_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ESP_BASE", str(tmp_path))
        registry, dev_dir, _ = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        mac_dir = tmp_path / "logs" / "ttyUSB0"
        mac_dir.mkdir(parents=True)
        (mac_dir / "mac").write_text("AA:BB:CC:DD:EE:FF")
        with patch("subprocess.run", side_effect=mock_tmux_down):
            device = registry.get_device("ttyUSB0")
        assert device.mac == "AA:BB:CC:DD:EE:FF"
        assert device.sn is not None
        assert device.sn.isdigit()

    def test_mac_isolation_across_devices(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ESP_BASE", str(tmp_path))
        registry, dev_dir, _ = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        (dev_dir / "ttyUSB1").touch()
        mac_dir = tmp_path / "logs" / "ttyUSB0"
        mac_dir.mkdir(parents=True)
        (mac_dir / "mac").write_text("11:22:33:44:55:66")
        with patch("subprocess.run", side_effect=mock_tmux_down):
            d1 = registry.get_device("ttyUSB1")
        assert d1.mac is None
        assert d1.sn is None


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
        (jobs_dir / "job_20260623_120000_board1_ttyUSB0").mkdir()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            device = registry.get_device("ttyUSB0")
        assert device.last_flash_ts == "2026-06-23T12:00:00"

    def test_last_flash_ts_picks_most_recent(self, tmp_path):
        registry, dev_dir, jobs_dir = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        (jobs_dir / "job_20260623_100000_board1_ttyUSB0").mkdir()
        (jobs_dir / "job_20260623_120000_board1_ttyUSB0").mkdir()
        (jobs_dir / "job_20260622_235900_board1_ttyUSB0").mkdir()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            device = registry.get_device("ttyUSB0")
        assert device.last_flash_ts == "2026-06-23T12:00:00"

    def test_last_flash_ts_per_device(self, tmp_path):
        """Regresion: cada device ve SU ultimo flasheo, no el mas reciente de cualquiera."""
        registry, dev_dir, jobs_dir = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        (dev_dir / "ttyUSB1").touch()
        (jobs_dir / "job_20260601_100000_board1_ttyUSB0").mkdir()
        (jobs_dir / "job_20260925_150000_board2_ttyUSB1").mkdir()  # mas reciente, otro device
        with patch("subprocess.run", side_effect=mock_tmux_down):
            d0 = registry.get_device("ttyUSB0")
            d1 = registry.get_device("ttyUSB1")
        assert d0.last_flash_ts == "2026-06-01T10:00:00"
        assert d1.last_flash_ts == "2026-09-25T15:00:00"

    def test_last_flash_ts_none_when_only_other_devices_flashed(self, tmp_path):
        """Device nunca flasheado no hereda la fecha de otro."""
        registry, dev_dir, jobs_dir = make_registry(tmp_path)
        (dev_dir / "ttyUSB0").touch()
        (jobs_dir / "job_20260925_150000_board2_ttyUSB1").mkdir()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            device = registry.get_device("ttyUSB0")
        assert device.last_flash_ts is None

    def test_last_flash_ts_ttyusb1_does_not_match_ttyusb10(self, tmp_path):
        registry, dev_dir, jobs_dir = make_registry(tmp_path)
        (dev_dir / "ttyUSB1").touch()
        (jobs_dir / "job_20260925_150000_board_ttyUSB10").mkdir()
        with patch("subprocess.run", side_effect=mock_tmux_down):
            device = registry.get_device("ttyUSB1")
        assert device.last_flash_ts is None

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


class TestDevicesFileIntegrity:
    """Regresion: devices.json se corrompia al escribir concurrentemente.

    Causa: f.write() solo llena el buffer de Python y el flush real ocurre al
    cerrar el archivo, DESPUES de soltar el flock. Dos procesos registrando MAC
    a la vez (devremote --reset levanta todos los devices juntos) intercalaban
    truncate y flush, dejando un JSON corto pegado a la cola del anterior.
    """

    def test_shrinking_write_leaves_no_tail(self, tmp_path):
        """Escribir un JSON mas corto sobre uno mas largo no debe dejar cola."""
        path = tmp_path / "devices.json"
        df = DevicesFile(path=path)

        for i in range(5):
            df.register_mac(f"AA:BB:CC:DD:EE:0{i}", f"sn{i}")
        assert len(json.loads(path.read_text())) == 5

        # Ahora una escritura que achica el archivo a una sola entrada.
        def _shrink(data):
            data.clear()
            data["AA:BB:CC:DD:EE:00"] = {"device_key": "solo", "hw_model": None}

        df._update(_shrink, silent=False)

        raw = path.read_text()
        data = json.loads(raw)  # explota con "Extra data" si quedo cola
        assert list(data) == ["AA:BB:CC:DD:EE:00"]
        assert raw.strip().endswith("}")

    def test_data_is_on_disk_before_lock_is_released(self, tmp_path, monkeypatch):
        """El invariante que rompia todo: cuando se suelta el flock, lo escrito
        ya tiene que estar en disco. Sin el flush+fsync, el buffer de Python se
        vacia recien al cerrar el archivo (despues del LOCK_UN), y otro proceso
        que tomaba el lock en el medio leia datos viejos y reescribia encima.
        """
        path = tmp_path / "devices.json"
        df = DevicesFile(path=path)
        df.register_mac("AA:BB:CC:DD:EE:00", "viejo")

        visto = {}
        real_flock = fcntl.flock

        def spy(fd, op):
            if op == fcntl.LOCK_UN and "contenido" not in visto:
                # Leer con otro descriptor, como haria otro proceso que
                # justo agarra el lock recien liberado.
                visto["contenido"] = path.read_text()
            return real_flock(fd, op)

        monkeypatch.setattr(fcntl, "flock", spy)
        df.register_mac("11:22:33:44:55:66", "nuevo")

        en_disco = json.loads(visto["contenido"])
        assert "11:22:33:44:55:66" in en_disco, (
            "al soltar el lock, el dato nuevo todavia no estaba en disco"
        )


class TestRuntimeState:
    """Fase 3: el proceso del device publica MAC y puerto en run/<tty>.json; los
    datos del flash viven en devices/<mac>/."""

    def _registry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ESP_BASE", str(tmp_path))
        return make_registry(tmp_path)

    def test_mac_and_port_from_runstate(self, tmp_path, monkeypatch):
        from server import runstate
        registry, dev_dir, _ = self._registry(tmp_path, monkeypatch)
        (dev_dir / "ttyUSB0").touch()
        runstate.write("ttyUSB0", {"mac": "AA:BB:CC:DD:EE:FF", "tcp_port": 5007, "state": "monitoring"})
        with patch("subprocess.run", side_effect=mock_tmux_down):
            d = registry.get_device("ttyUSB0")
        assert d.mac == "AA:BB:CC:DD:EE:FF" and d.port_tcp == 5007 and d.sn

    def test_runstate_wins_over_legacy_mac_file(self, tmp_path, monkeypatch):
        from server import runstate
        registry, dev_dir, _ = self._registry(tmp_path, monkeypatch)
        (dev_dir / "ttyUSB0").touch()
        (tmp_path / "logs" / "ttyUSB0").mkdir(parents=True)
        (tmp_path / "logs" / "ttyUSB0" / "mac").write_text("11:22:33:44:55:66")
        runstate.write("ttyUSB0", {"mac": "AA:BB:CC:DD:EE:FF"})
        with patch("subprocess.run", side_effect=mock_tmux_down):
            assert registry.get_device("ttyUSB0").mac == "AA:BB:CC:DD:EE:FF"

    def test_last_flash_from_device_jobs_and_legacy(self, tmp_path, monkeypatch):
        from server import runstate
        registry, dev_dir, jobs_dir = self._registry(tmp_path, monkeypatch)
        (dev_dir / "ttyUSB0").touch()
        runstate.write("ttyUSB0", {"mac": "AA:BB:CC:DD:EE:FF"})
        (tmp_path / "devices" / "AABBCCDDEEFF" / "jobs" / "job_20261005_120000_board1").mkdir(parents=True)
        (jobs_dir / "job_20260101_090000_board1_ttyUSB0").mkdir()   # esquema anterior
        with patch("subprocess.run", side_effect=mock_tmux_down):
            assert registry.get_device("ttyUSB0").last_flash_ts == "2026-10-05T12:00:00"

    def test_last_user_from_device_home(self, tmp_path, monkeypatch):
        from server import runstate
        registry, dev_dir, _ = self._registry(tmp_path, monkeypatch)
        (dev_dir / "ttyUSB0").touch()
        runstate.write("ttyUSB0", {"mac": "AA:BB:CC:DD:EE:FF"})
        home = tmp_path / "devices" / "AABBCCDDEEFF"
        home.mkdir(parents=True)
        (home / "last_user").write_text("alejo")
        with patch("subprocess.run", side_effect=mock_tmux_down):
            assert registry.get_device("ttyUSB0").last_flash_user == "alejo"

    def test_get_device_by_key_via_runstate(self, tmp_path, monkeypatch):
        from server import runstate
        registry, dev_dir, _ = self._registry(tmp_path, monkeypatch)
        (dev_dir / "ttyUSB0").touch()
        runstate.write("ttyUSB0", {"mac": "AA:BB:CC:DD:EE:FF"})
        registry._devices_file.update_device_key("AA:BB:CC:DD:EE:FF", "OEM_NOVUS")
        with patch("subprocess.run", side_effect=mock_tmux_down):
            d = registry.get_device_by_key("OEM_NOVUS")
        assert d is not None and d.tty_name == "ttyUSB0"

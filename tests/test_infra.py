"""
Tests de los scripts de infra (bash) sin hardware: espbench-name (regla de
nombres y puertos), esp32_tmux.sh y devremote. tmux, udevadm, pkill y sudo
son fakes en el PATH; los scripts son los del repo.
"""
import os
import pathlib
import subprocess

import pytest

INFRA = pathlib.Path(__file__).parent.parent / "remote" / "infra"

FAKES = {
    "udevadm": r'''#!/bin/bash
# udevadm info -q property -n <dev>
dev="${@: -1}"; f="$FAKE/idpath/$(basename "$dev")"
[ -f "$f" ] && echo "ID_PATH=$(cat "$f")"
exit 0
''',
    "tmux": r'''#!/bin/bash
echo "$*" >> "$FAKE/tmux.log"
case "$1" in
  has-session)  [ -f "$FAKE/sessions/$3" ] ;;
  new-session)  touch "$FAKE/sessions/$4"; echo "${@: -1}" > "$FAKE/sessions/$4" ;;
  kill-session) rm -f "$FAKE/sessions/$3" ;;
  ls)           for s in "$FAKE"/sessions/*; do
                  [ -e "$s" ] || continue
                  if [ "${2:-}" = "-F" ]; then basename "$s"; else echo "$(basename "$s"): 1 windows"; fi
                done ;;
  list-panes)   echo 4242 ;;
esac
''',
    "pkill": '#!/bin/bash\necho "$*" >> "$FAKE/pkill.log"\nexit 0\n',
    "sudo": '#!/bin/bash\n"$@"\n',
    "sleep": "#!/bin/bash\nexit 0\n",
}


@pytest.fixture
def infra(tmp_path):
    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    (fake / "sessions").mkdir()
    (fake / "idpath").mkdir()
    for name, body in FAKES.items():
        f = fake / "bin" / name
        f.write_text(body)
        f.chmod(0o755)
    base, devdir = tmp_path / "esp", tmp_path / "dev"
    (base / "run").mkdir(parents=True)
    (base / "locks").mkdir()
    devdir.mkdir()
    env = {
        "PATH": f"{fake / 'bin'}:{INFRA}:/usr/bin:/bin",
        "ESP_BASE": str(base), "ESPBENCH_DEV_DIR": str(devdir),
        "DEVREMOTE_NO_REEXEC": "1", "FAKE": str(fake),
    }

    class Infra:
        def run(self, script, *args, check=True):
            r = subprocess.run(["bash", str(INFRA / script), *args], env=env,
                               capture_output=True, text=True)
            if check:
                assert r.returncode == 0, r.stderr
            return r

        def plug(self, kname, id_path=None):
            (devdir / kname).touch()
            if id_path:
                (fake / "idpath" / kname).write_text(id_path)

        def slots(self, text):
            (base / "slots.conf").write_text(text)

        def session_cmd(self, name):
            f = fake / "sessions" / name
            return f.read_text() if f.exists() else None

        def log(self, which):
            f = fake / f"{which}.log"
            return f.read_text() if f.exists() else ""

    i = Infra()
    i.base, i.devdir, i.fake = base, devdir, fake
    return i


HUB_2 = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2:1.0"
HUB_3 = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0"


# ---------- espbench-name: la regla de nombres y puertos ----------

def test_name_without_slots_is_ttyusb_as_always(infra):
    assert infra.run("espbench-name", "ttyUSB3").stdout.split() == ["ttyUSB3", "5003"]
    assert infra.run("espbench-name", "/dev/ttyUSB0").stdout.split() == ["ttyUSB0", "5000"]


def test_name_mapped_slot(infra):
    infra.slots(f"# hub de la mesa 1\n2 {HUB_2}\n\n3 {HUB_3}\n")
    infra.plug("ttyUSB7", HUB_2)
    assert infra.run("espbench-name", "ttyUSB7").stdout.split() == ["esp-slot2", "5002"]


def test_name_unmapped_device_when_slots_exist_does_not_collide(infra):
    infra.slots(f"2 {HUB_2}\n")
    infra.plug("ttyUSB2", "platform-otro-puerto")
    assert infra.run("espbench-name", "ttyUSB2").stdout.split() == ["ttyUSB2", "5102"]


def test_name_only_comments_counts_as_no_slots(infra):
    infra.slots("# todavía sin mapear\n\n")
    assert infra.run("espbench-name", "ttyUSB2").stdout.split() == ["ttyUSB2", "5002"]


def test_name_of_slot_device_itself(infra):
    assert infra.run("espbench-name", "/dev/esp-slot5").stdout.split() == ["esp-slot5", "5005"]


def test_slot_for_path_used_by_udev(infra):
    infra.slots(f"2 {HUB_2}\n")
    assert infra.run("espbench-name", "--slot-for-path", HUB_2).stdout.strip() == "esp-slot2"
    assert infra.run("espbench-name", "--slot-for-path", HUB_3, check=False).returncode == 1
    assert infra.run("espbench-name", "--slot-for-path", "", check=False).returncode == 1


# ---------- esp32_tmux.sh ----------

def test_tmux_creates_session_with_name_port_and_base(infra):
    infra.plug("ttyUSB3")
    infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB3"))
    cmd = infra.session_cmd("esp32_ttyUSB3")
    assert f"-p {infra.devdir}/ttyUSB3 -tcp 5003 --base {infra.base}" in cmd
    assert "pipe-pane" not in infra.log("tmux")


def test_tmux_uses_slot_device_path(infra):
    infra.slots(f"2 {HUB_2}\n")
    infra.plug("ttyUSB7", HUB_2)
    (infra.devdir / "esp-slot2").touch()       # el symlink que crea udev
    infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB7"))
    cmd = infra.session_cmd("esp32_esp-slot2")
    assert f"-p {infra.devdir}/esp-slot2 -tcp 5002" in cmd


def test_tmux_fails_clearly_if_slot_symlink_missing(infra):
    infra.slots(f"2 {HUB_2}\n")
    infra.plug("ttyUSB7", HUB_2)
    r = infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB7"), check=False)
    assert r.returncode == 1 and "esp-slot2 no existe" in r.stderr


def test_tmux_keeps_running_session(infra):
    infra.plug("ttyUSB3")
    (infra.fake / "sessions" / "esp32_ttyUSB3").write_text("viejo")
    (infra.base / "run" / "ttyUSB3.json").write_text('{\n  "state": "monitoring"\n}')
    infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB3"))
    assert infra.session_cmd("esp32_ttyUSB3") == "viejo"


def test_tmux_recreates_session_of_disconnected_device(infra):
    infra.plug("ttyUSB3")
    (infra.fake / "sessions" / "esp32_ttyUSB3").write_text("viejo")
    (infra.base / "run" / "ttyUSB3.json").write_text('{\n  "state": "disconnected"\n}')
    infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB3"))
    assert "kill-session -t esp32_ttyUSB3" in infra.log("tmux")
    assert "remote_esp32.py" in infra.session_cmd("esp32_ttyUSB3")


def test_tmux_releases_lock_on_new_session(infra):
    infra.plug("ttyUSB3")
    (infra.base / "locks" / "ttyUSB3").write_text("alejo:t0k")
    infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB3"))
    assert not (infra.base / "locks" / "ttyUSB3").exists()


def test_tmux_keeps_reservation_on_new_session(infra):
    """Una reserva (con vencimiento) sobrevive un replug; remote_esp32.py la
    borra al arrancar si en el puerto quedó otra placa."""
    infra.plug("ttyUSB3")
    (infra.base / "locks" / "ttyUSB3").write_text("alejo:t0k:4102444800:AABBCCDDEEFF")
    (infra.base / "locks" / "ttyUSB4").write_text("alejo:t0k:4102444800")
    infra.plug("ttyUSB4")
    infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB3"))
    infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB4"))
    assert (infra.base / "locks" / "ttyUSB3").read_text() == "alejo:t0k:4102444800:AABBCCDDEEFF"
    assert (infra.base / "locks" / "ttyUSB4").exists()


@pytest.mark.parametrize("content,kept", [
    ("alejo:t0k", False),
    ("alejo:t0k:4102444800", True),
    ("alejo:t0k:4102444800:aabbccddeeff\n", True),
    ("alejo:a:b", False),                 # token viejo con ':': permanente (locks.parse igual)
    ("alejo:a:b:4102444800", False),
    ("alejo:t0k:4102444800:xyz", False),
])
def test_tmux_lock_rule_matches_locks_parse(infra, content, kept):
    import sys
    sys.path.insert(0, str(INFRA.parent))
    from server import locks
    infra.plug("ttyUSB3")
    (infra.base / "locks" / "ttyUSB3").write_text(content)
    infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB3"))
    assert (infra.base / "locks" / "ttyUSB3").exists() == kept
    assert locks.parse(content).reservation == kept


# ---------- devremote ----------

def test_devremote_scan_starts_missing(infra):
    infra.plug("ttyUSB0")
    infra.plug("ttyUSB1")
    (infra.fake / "sessions" / "esp32_ttyUSB0").write_text("ya corre")
    infra.run("devremote")
    assert infra.session_cmd("esp32_ttyUSB0") == "ya corre"
    assert "-tcp 5001" in infra.session_cmd("esp32_ttyUSB1")


def test_devremote_start_used_by_hotplug(infra):
    infra.plug("ttyUSB4")
    infra.run("devremote", "--start", "ttyUSB4")
    assert "-tcp 5004" in infra.session_cmd("esp32_ttyUSB4")


def test_devremote_reset_one_does_not_kill_ttyusb10(infra):
    infra.plug("ttyUSB1")
    infra.run("devremote", "--reset", "1")
    pattern = infra.log("pkill").rstrip("\n")
    assert pattern.endswith(f"-p {infra.devdir}/ttyUSB1 ")   # el espacio final excluye ttyUSB10
    assert "esp32_ttyUSB1" in infra.log("tmux")


def test_devremote_reset_accepts_slot_names(infra):
    infra.slots(f"2 {HUB_2}\n")
    infra.plug("ttyUSB7", HUB_2)
    (infra.devdir / "esp-slot2").touch()
    infra.run("devremote", "--reset", "slot2")
    assert f"-p {infra.devdir}/esp-slot2 " in infra.log("pkill")
    assert "remote_esp32.py" in infra.session_cmd("esp32_esp-slot2")


def test_devremote_unlock_by_number_and_slot(infra):
    (infra.base / "locks" / "ttyUSB3").write_text("alejo:t0k")
    (infra.base / "locks" / "esp-slot2").write_text("juan:x")
    assert "alejo" in infra.run("devremote", "--unlock", "3").stdout
    assert "juan" in infra.run("devremote", "--unlock", "esp-slot2").stdout
    assert list((infra.base / "locks").iterdir()) == []


def test_devremote_rejects_weird_names(infra):
    r = infra.run("devremote", "--unlock", "../../etc", check=False)
    assert r.returncode != 0 and "inválido" in r.stderr


def test_devremote_status_shows_name_port_and_fsm_state(infra):
    infra.slots(f"2 {HUB_2}\n")
    infra.plug("ttyUSB7", HUB_2)
    (infra.fake / "sessions" / "esp32_esp-slot2").touch()
    (infra.base / "run" / "esp-slot2.json").write_text('{\n  "state": "flashing"\n}')
    out = infra.run("devremote", "--status").stdout
    row = [l for l in out.splitlines() if l.startswith("esp-slot2")][0].split()
    assert row[:5] == ["esp-slot2", "ttyUSB7", "5002", "RUNNING", "flashing"]


def test_devremote_slots_lists_id_paths(infra):
    infra.plug("ttyUSB0", HUB_3)
    out = infra.run("devremote", "--slots").stdout
    assert HUB_3 in out and "slots.conf" in out


def test_devremote_reset_all_restarts_when_no_session_survives(infra):
    """Regresión: lo normal es que pkill -9 mate el proceso y con él la sesión.
    tmux ls queda vacío, grep sale con 1 y con set -e + pipefail devremote
    moría sin relanzar nada: --reset dejaba la Pi sin sesiones."""
    infra.plug("ttyUSB0")
    infra.plug("ttyUSB1")
    r = infra.run("devremote", "--reset", check=False)
    assert r.returncode == 0, r.stderr
    assert infra.session_cmd("esp32_ttyUSB0") and infra.session_cmd("esp32_ttyUSB1")


def test_devremote_reset_all_kills_surviving_sessions(infra):
    infra.plug("ttyUSB0")
    (infra.fake / "sessions" / "esp32_ttyUSB0").write_text("viejo")
    (infra.fake / "sessions" / "otra_cosa").write_text("no tocar")
    infra.run("devremote", "--reset")
    assert "kill-session -t esp32_ttyUSB0" in infra.log("tmux")
    assert "otra_cosa" not in infra.log("tmux").replace("ls -F", "")
    assert "remote_esp32.py" in infra.session_cmd("esp32_ttyUSB0")


def test_devremote_service_waits_for_time_sync():
    """La Pi no tiene RTC: las sesiones del boot no arrancan con la hora de
    fake-hwclock (session_id y horas del log). Lo demás solo se ve en la Pi."""
    unit = (INFRA / "devremote.service").read_text().splitlines()
    assert "After=time-sync.target" in unit and "Wants=time-sync.target" in unit
    attach = (INFRA / "espbench-attach@.service").read_text().splitlines()
    assert "After=devremote.service" in attach      # al boot, el hotplug también espera
    install = (INFRA.parent / "install.sh").read_text()
    assert "systemctl enable systemd-time-wait-sync.service" in install
    assert "TimeoutStartSec=90" in install           # sin red no se cuelga el boot


# ---------- install.sh: dependencias Python ----------

@pytest.fixture
def fake_pip(tmp_path):
    """pip falso: anota cada llamada (y el contenido del -r) y falla con los
    paquetes de $PIP_FAIL."""
    pip = tmp_path / "pip"
    pip.write_text(r'''#!/bin/bash
echo "$*" >> "$PIP_LOG"
if [ "$2" = "--quiet" ] && [ "$3" = "-r" ]; then cat "$4" >> "$PIP_LOG"; fi
for p in $PIP_FAIL; do [ "${@: -1}" = "$p" ] && exit 1; done
exit 0
''')
    pip.chmod(0o755)
    return pip


def _pip_deps(tmp_path, pip, fail=""):
    log = tmp_path / "pip.log"
    env = {**os.environ, "PIP_LOG": str(log), "PIP_FAIL": fail}
    r = subprocess.run(["bash", str(INFRA / "pip-deps.sh"), str(pip), str(INFRA.parent / "requirements.txt")],
                       env=env, capture_output=True, text=True)
    return r, log.read_text() if log.exists() else ""


def test_install_regex_failure_does_not_abort(tmp_path, fake_pip):
    """install.sh corre con set -e: si `regex` no se instala (sin wheel ni
    compilador), avisa y sigue; logrange tiene fallback."""
    r, log = _pip_deps(tmp_path, fake_pip, fail="regex")
    assert r.returncode == 0, r.stderr
    assert "no se pudo instalar 'regex'" in r.stderr
    req_part = log.split("install --quiet regex")[0]
    assert "fastapi" in req_part and "esptool" in req_part and "regex" not in req_part
    assert 'pip-deps.sh" /opt/esp/venv/bin/pip' in (INFRA.parent / "install.sh").read_text()


def test_install_required_dependency_failure_still_aborts(tmp_path, fake_pip):
    fake_pip.write_text("#!/bin/bash\nexit 1\n")
    r, _ = _pip_deps(tmp_path, fake_pip)
    assert r.returncode != 0

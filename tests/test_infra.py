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
  new-session)  cmd="${@: -1}"
                tty="$(echo "$cmd" | sed -n 's/.* -p \([^ ]*\) .*/\1/p')"
                # ¿arranca con el proceso viejo de esa placa todavía vivo?
                old="$(espbench-procs --roots "$tty")"
                [ -n "$old" ] && echo "OLD_ALIVE $tty $(echo $old)" >> "$FAKE/tmux.log"
                echo "$cmd" > "$FAKE/sessions/$4"
                # el comando de la sesión: sudo → python (remote_esp32.py)
                n=$(( $(cat "$FAKE/nextpid" 2>/dev/null || echo 7000) + 2 )); echo "$n" > "$FAKE/nextpid"
                echo "$n 1 S $cmd" >> "$FAKE/ps.table"
                echo "$((n + 1)) $n S ${cmd#sudo }" >> "$FAKE/ps.table"
                echo "$n $((n + 1))" > "$FAKE/sesspid/$4" ;;
  kill-session) rm -f "$FAKE/sessions/$3"
                # SIGHUP a los procesos de la sesión: mueren
                for p in $(cat "$FAKE/sesspid/$3" 2>/dev/null); do
                  grep -v "^$p " "$FAKE/ps.table" > "$FAKE/ps.tmp" || true; mv "$FAKE/ps.tmp" "$FAKE/ps.table"
                done
                rm -f "$FAKE/sesspid/$3" ;;
  ls)           n=0                      # sin sesiones no hay server: tmux ls sale con 1
                for s in "$FAKE"/sessions/*; do
                  [ -e "$s" ] || continue
                  n=$((n + 1))
                  if [ "${2:-}" = "-F" ]; then basename "$s"; else echo "$(basename "$s"): 1 windows"; fi
                done
                [ "$n" -gt 0 ] ;;
  list-panes)   echo 4242 ;;
esac
''',
    "pkill": '#!/bin/bash\necho "$*" >> "$FAKE/pkill.log"\nexit 0\n',
    # Tabla de procesos falsa ($FAKE/ps.table: "pid ppid stat args"). Un pid en dying/<pid>
    # (contador) sigue apareciendo esa cantidad de llamadas más: un proceso que tarda en morir.
    "ps": r'''#!/bin/bash
t="$FAKE/ps.table"; touch "$t"
for d in "$FAKE"/dying/*; do
  [ -e "$d" ] || continue
  n=$(cat "$d"); pid=$(basename "$d")
  if [ "$n" -le 0 ]; then
    rm -f "$d"; grep -v "^$pid " "$t" > "$t.tmp" || true; mv "$t.tmp" "$t"
  else
    echo $((n - 1)) > "$d"
  fi
done
cat "$t"
''',
    # kill -9 pids: los saca de la tabla. immortal/<pid> (contador): sobrevive esa cantidad de
    # kills. slow/<pid> (contador): muere, pero sigue en la tabla esa cantidad de ps más.
    "kill": r'''#!/bin/bash
echo "kill $*" >> "$FAKE/kill.log"
t="$FAKE/ps.table"
for pid in "$@"; do
  case "$pid" in -*) continue ;; esac
  if [ -f "$FAKE/immortal/$pid" ] && [ "$(cat "$FAKE/immortal/$pid")" -gt 0 ]; then
    echo $(( $(cat "$FAKE/immortal/$pid") - 1 )) > "$FAKE/immortal/$pid"; continue
  fi
  if [ -f "$FAKE/slow/$pid" ]; then mv "$FAKE/slow/$pid" "$FAKE/dying/$pid"; continue; fi
  grep -v "^$pid " "$t" > "$t.tmp" || true; mv "$t.tmp" "$t"
done
''',
    # sudo [-n] [-u user] cmd...: exec, así `sudo kill` usa el kill falso y no el builtin
    "sudo": r'''#!/bin/bash
while [ $# -gt 0 ]; do
  case "$1" in -u) shift 2 ;; -*) shift ;; *) break ;; esac
done
exec "$@"
''',
    # systemd-run --scope ... -- cmd: anota los argumentos y corre cmd (FAKE_SYSTEMD_RUN_FAIL: falla)
    "systemd-run": r'''#!/bin/bash
echo "$*" >> "$FAKE/systemd-run.log"
[ -f "$FAKE/systemd-run.fail" ] && exit 1
while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done
shift
exec "$@"
''',
    "sleep": "#!/bin/bash\nexit 0\n",
}


@pytest.fixture
def infra(tmp_path):
    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    for d in ("sessions", "idpath", "sesspid", "dying", "immortal", "slow"):
        (fake / d).mkdir()
    (fake / "ps.table").touch()
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

        def proc(self, pid, ppid, args, stat="S"):
            """Un proceso en la tabla falsa de ps."""
            with open(fake / "ps.table", "a") as f:
                f.write(f"{pid} {ppid} {stat} {args}\n")

        def board(self, name, pid=100, session=True):
            """Una placa corriendo como en la Pi: tmux server (cuyo cmdline es el del new-session
            que lo creó), sudo, el python de remote_esp32.py y un esptool hijo. Devuelve los pids."""
            cmd = f"sudo {base}/venv/bin/python3 {base}/server/remote_esp32.py -p {devdir}/{name} -tcp 5000 --base {base}"
            self.proc(pid, 1, f"tmux new-session -d -s esp32_{name} {cmd}")
            self.proc(pid + 1, pid, cmd)
            self.proc(pid + 2, pid + 1, cmd[len("sudo "):])
            self.proc(pid + 3, pid + 2, f"{base}/venv/bin/python3 -m esptool --port {devdir}/{name} read_mac")
            if session:
                (fake / "sessions" / f"esp32_{name}").write_text("viejo")
                (fake / "sesspid" / f"esp32_{name}").write_text(f"{pid + 1} {pid + 2}")
            return pid + 1, pid + 2, pid + 3

        def pids(self):
            return {int(l.split()[0]) for l in (fake / "ps.table").read_text().splitlines() if l.strip()}

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
    infra.board("ttyUSB3")
    (infra.base / "run" / "ttyUSB3.json").write_text('{\n  "state": "monitoring"\n}')
    infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB3"))
    assert infra.session_cmd("esp32_ttyUSB3") == "viejo"


def test_tmux_recreates_session_whose_process_is_gone(infra):
    """La sesión existe pero su remote_esp32 ya no: antes se la salteaba (has-session) y,
    cuando la sesión terminaba de cerrarse, el device quedaba sin ninguna."""
    infra.plug("ttyUSB3")
    (infra.fake / "sessions" / "esp32_ttyUSB3").write_text("viejo")
    (infra.base / "run" / "ttyUSB3.json").write_text('{\n  "state": "discovering"\n}')
    infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB3"))
    assert "kill-session -t esp32_ttyUSB3" in infra.log("tmux")
    assert "remote_esp32.py" in infra.session_cmd("esp32_ttyUSB3")


def test_tmux_waits_for_a_stopping_process_and_recreates(infra):
    """El proceso recibió una señal y está cerrando ("stopping"): se espera a que salga y
    se recrea la sesión, en vez de saltearla porque todavía está vivo."""
    infra.plug("ttyUSB3")
    sudo, py, esptool = infra.board("ttyUSB3")
    for pid in (sudo, py):
        (infra.fake / "dying" / str(pid)).write_text("3")       # sale solo, en un rato
    (infra.base / "run" / "ttyUSB3.json").write_text('{\n  "state": "monitoring",\n  "stopping": true\n}')
    infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB3"))
    assert "OLD_ALIVE" not in infra.log("tmux")
    assert "remote_esp32.py" in infra.session_cmd("esp32_ttyUSB3")


def test_tmux_recreates_session_of_disconnected_device(infra):
    infra.plug("ttyUSB3")
    (infra.fake / "sessions" / "esp32_ttyUSB3").write_text("viejo")
    (infra.base / "run" / "ttyUSB3.json").write_text('{\n  "state": "disconnected"\n}')
    infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB3"))
    assert "kill-session -t esp32_ttyUSB3" in infra.log("tmux")
    assert "remote_esp32.py" in infra.session_cmd("esp32_ttyUSB3")


def test_tmux_first_session_starts_the_server_in_its_own_scope(infra):
    """Sin tmux server, el new-session lo crea en el cgroup de quien llama. Desde el unit
    del update (systemd-run) o desde dashboard.service, systemd lo mataba con todas las
    sesiones al terminar el update o reiniciar el dashboard. Va en un scope propio."""
    infra.plug("ttyUSB3")
    infra.plug("ttyUSB4")
    infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB3"))
    scope = infra.log("systemd-run")
    assert "--scope" in scope and f"--uid={os.getuid()}" in scope
    assert "-- tmux new-session -d -s esp32_ttyUSB3" in scope
    assert "remote_esp32.py" in infra.session_cmd("esp32_ttyUSB3")
    # con el server ya corriendo, las sesiones siguientes van a ese server: sin scope
    infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB4"))
    assert infra.log("systemd-run").count("--scope") == 1
    assert "remote_esp32.py" in infra.session_cmd("esp32_ttyUSB4")


def test_tmux_without_scope_still_starts_the_session(infra):
    infra.plug("ttyUSB3")
    (infra.fake / "systemd-run.fail").touch()
    r = infra.run("esp32_tmux.sh", str(infra.devdir / "ttyUSB3"))
    assert "scope falló" in r.stderr
    assert "remote_esp32.py" in infra.session_cmd("esp32_ttyUSB3")


def test_update_units_do_not_kill_the_sessions_they_leave():
    """Red de seguridad si el scope falla: el unit del update no mata lo que deja corriendo."""
    unit = (INFRA / "espbench-update.service").read_text().splitlines()
    assert "KillMode=process" in unit
    import sys
    sys.path.insert(0, str(INFRA.parent))
    from server import update
    cmd = update.start_command("feat/x", unit="u")
    assert cmd[cmd.index("-p") + 1] == "KillMode=process" and cmd.index("-p") < cmd.index(update.UPDATE_BIN)


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
    ("alejo:a%3A1", False),               # token con ':' escapado (flash): permanente
    ("alejo:a%3A1:4102444800:aabbccddeeff", True),
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
    infra.board("ttyUSB0")
    infra.run("devremote")
    assert infra.session_cmd("esp32_ttyUSB0") == "viejo"
    assert "-tcp 5001" in infra.session_cmd("esp32_ttyUSB1")


def test_devremote_start_used_by_hotplug(infra):
    infra.plug("ttyUSB4")
    infra.run("devremote", "--start", "ttyUSB4")
    assert "-tcp 5004" in infra.session_cmd("esp32_ttyUSB4")


def test_devremote_reset_one_does_not_kill_ttyusb10(infra):
    infra.plug("ttyUSB1")
    infra.plug("ttyUSB10")
    b1 = infra.board("ttyUSB1", pid=100)
    b10 = infra.board("ttyUSB10", pid=200)
    infra.run("devremote", "--reset", "1")
    alive = infra.pids()
    assert not alive & set(b1)                   # sudo, python y el esptool hijo
    assert set(b10) <= alive                     # ttyUSB10 no se toca
    assert {100, 200} <= alive                   # ni el tmux server (su cmdline lleva el comando entero)
    assert "OLD_ALIVE" not in infra.log("tmux")
    assert infra.session_cmd("esp32_ttyUSB1") != "viejo" and infra.session_cmd("esp32_ttyUSB10") == "viejo"


def test_devremote_reset_accepts_slot_names(infra):
    infra.slots(f"2 {HUB_2}\n")
    infra.plug("ttyUSB7", HUB_2)
    (infra.devdir / "esp-slot2").touch()
    pids = infra.board("esp-slot2")
    infra.run("devremote", "--reset", "slot2")
    assert not infra.pids() & set(pids)
    assert "remote_esp32.py" in infra.session_cmd("esp32_esp-slot2")


def test_devremote_reset_waits_until_the_old_process_is_really_gone(infra):
    """kill -9 no es instantáneo (un proceso en medio de I/O USB tarda en morir): la sesión
    nueva no arranca con el proceso viejo de la placa todavía vivo."""
    infra.plug("ttyUSB1")
    sudo, py, esptool = infra.board("ttyUSB1")
    (infra.fake / "slow" / str(py)).write_text("4")
    infra.run("devremote", "--reset", "1")
    assert "OLD_ALIVE" not in infra.log("tmux")
    assert "remote_esp32.py" in infra.session_cmd("esp32_ttyUSB1")


def test_devremote_reset_kills_again_if_the_first_kill_does_not_take(infra):
    infra.plug("ttyUSB1")
    sudo, py, esptool = infra.board("ttyUSB1")
    (infra.fake / "immortal" / str(py)).write_text("1")       # sobrevive al primer -9
    r = infra.run("devremote", "--reset", "1")
    kills = infra.log("kill").splitlines()
    assert len(kills) == 2 and str(py) in kills[1].split()
    assert "siguen vivos" in r.stderr
    assert "OLD_ALIVE" not in infra.log("tmux")


def test_devremote_reset_goes_on_if_the_process_never_dies(infra):
    infra.plug("ttyUSB1")
    sudo, py, esptool = infra.board("ttyUSB1")
    (infra.fake / "immortal" / str(py)).write_text("99")
    r = infra.run("devremote", "--reset", "1")
    assert "no murieron" in r.stderr
    assert "remote_esp32.py" in infra.session_cmd("esp32_ttyUSB1")


def test_devremote_unlock_by_number_and_slot(infra):
    (infra.base / "locks" / "ttyUSB3").write_text("alejo:t0k")
    (infra.base / "locks" / "esp-slot2").write_text("juan:x")
    assert "alejo" in infra.run("devremote", "--unlock", "3").stdout
    assert "juan" in infra.run("devremote", "--unlock", "esp-slot2").stdout
    assert list((infra.base / "locks").iterdir()) == []


def test_devremote_unlock_takes_the_flock_and_records_release(infra):
    """Con el server instalado, --unlock pasa por locks.exclusive (el mismo flock
    del api y del flash) y deja un evento release forzado con el dueño anterior."""
    import json
    import os
    (infra.base / "server").symlink_to(INFRA.parent / "server")
    sid = "20261006_120000_7"
    home = infra.base / "devices" / "AABBCCDDEEFF"
    home.mkdir(parents=True)
    (home / "output.log").write_text(f"2026-10-06 12:00:00.000 | INFO  | devicelog      | sesión {sid} tty=ttyUSB3\n")
    (infra.base / "run" / "ttyUSB3.json").write_text(json.dumps({"log_path": str(home / "output.log"),
                                                                   "mac": "AA:BB:CC:DD:EE:FF"}))
    (infra.base / "locks" / "ttyUSB3").write_text("al%3Aejo:t0k:4102444800:AABBCCDDEEFF")
    r = infra.run("devremote", "--unlock", "3")
    assert "era de: al:ejo" in r.stdout
    assert sorted(os.listdir(infra.base / "locks")) == ["ttyUSB3.lck"]          # el flock de locks.exclusive
    (ev,) = [json.loads(l) for l in (home / "events.jsonl").read_text().splitlines()]
    assert ev["type"] == "release" and ev["by"] == "devremote" and ev["cursor"].startswith(f"c:{sid}:")
    assert ev["detail"]["user"] == "al:ejo" and ev["detail"]["forced"] and ev["detail"]["by_host"] == "devremote"
    assert "No había lock" in infra.run("devremote", "--unlock", "3").stdout


def test_devremote_rejects_weird_names(infra):
    r = infra.run("devremote", "--unlock", "../../etc", check=False)
    assert r.returncode != 0 and "inválido" in r.stderr


def test_devremote_status_shows_name_port_and_fsm_state(infra):
    infra.slots(f"2 {HUB_2}\n")
    infra.plug("ttyUSB7", HUB_2)
    infra.board("esp-slot2")
    (infra.base / "run" / "esp-slot2.json").write_text('{\n  "state": "flashing"\n}')
    infra.plug("ttyUSB8")
    (infra.fake / "sessions" / "esp32_ttyUSB8").write_text("sin proceso")
    out = infra.run("devremote", "--status").stdout
    row = [l for l in out.splitlines() if l.startswith("esp-slot2")][0].split()
    assert row[:5] == ["esp-slot2", "ttyUSB7", "5002", "RUNNING", "flashing"]
    row = [l for l in out.splitlines() if l.startswith("ttyUSB8")][0].split()
    assert row[3] == "STALE"


def test_devremote_check_needs_a_live_session_per_device(infra):
    infra.plug("ttyUSB0")
    infra.plug("ttyUSB1")
    infra.plug("ttyUSB2")
    infra.board("ttyUSB0", pid=100)
    (infra.fake / "sessions" / "esp32_ttyUSB1").write_text("sin proceso")
    r = infra.run("devremote", "--check", check=False)
    assert r.returncode == 1
    assert "ttyUSB0: ok" in r.stdout and "ttyUSB1: sesión sin proceso" in r.stdout
    assert "ttyUSB2: sin sesión" in r.stdout
    infra.run("devremote")
    assert infra.run("devremote", "--check").returncode == 0


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


def test_devremote_reset_all_kills_every_board_and_its_children(infra):
    infra.plug("ttyUSB0")
    infra.plug("ttyUSB1")
    b0 = infra.board("ttyUSB0", pid=100)
    b1 = infra.board("ttyUSB1", pid=200)
    infra.proc(300, 1, "/usr/bin/python3 /home/sfypi/otra_cosa.py")
    infra.run("devremote", "--reset")
    alive = infra.pids()
    assert not alive & (set(b0) | set(b1))
    assert {100, 200, 300} <= alive              # tmux servers y lo que no es de espbench
    assert "OLD_ALIVE" not in infra.log("tmux")
    assert infra.session_cmd("esp32_ttyUSB0") != "viejo" and infra.session_cmd("esp32_ttyUSB1") != "viejo"


def test_devremote_reset_all_kills_surviving_sessions(infra):
    infra.plug("ttyUSB0")
    (infra.fake / "sessions" / "esp32_ttyUSB0").write_text("viejo")
    (infra.fake / "sessions" / "otra_cosa").write_text("no tocar")
    infra.run("devremote", "--reset")
    assert "kill-session -t esp32_ttyUSB0" in infra.log("tmux")
    assert "otra_cosa" not in infra.log("tmux").replace("ls -F", "")
    assert "remote_esp32.py" in infra.session_cmd("esp32_ttyUSB0")


def test_install_installs_every_script_the_sessions_use():
    install = (INFRA.parent / "install.sh").read_text()
    for script in ("devremote", "esp32_tmux.sh", "espbench-name", "espbench-procs"):
        assert f'cp "$REMOTE_DIR/infra/{script}" /usr/local/bin/{script}' in install
        assert os.access(INFRA / script, os.X_OK)


def test_install_creates_meta_dir_writable_by_the_api():
    """properties.json lo escribe el api (sfypi) con archivo temporal + rename: su directorio
    tiene que ser escribible (/opt/esp es root 755)."""
    install = (INFRA.parent / "install.sh").read_text()
    assert "mkdir -p /opt/esp/meta" in install and "chmod 777 /opt/esp/meta" in install
    assert "mv /opt/esp/properties.json /opt/esp/meta/properties.json" in install


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

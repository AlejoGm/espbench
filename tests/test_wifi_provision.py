"""
Tests de rpi/wifi-provision.sh sin hardware: nmcli y wifi-connect son fakes en el PATH.

Regla: el AP (que en la Pi 4 tira abajo la conexión WiFi del bench) sale solo si no hay ninguna red
guardada o si wlan0 lleva GRACE_S seguidos sin red. Un corte de internet no cuenta.
"""
import os
import pathlib
import shlex
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).parent.parent / "rpi" / "wifi-provision.sh"

# $FAKE/state: estado de wlan0 en `nmcli -t -f DEVICE,STATE device`.
# $FAKE/conns: salida de `nmcli -t -f NAME,TYPE connection show`.
FAKES = {
    "nmcli": r'''#!/bin/bash
echo "$*" >> "$FAKE/nmcli.log"
case "$*" in
  "-t -f DEVICE,STATE device")      echo "wlan0:$(cat "$FAKE/state")"; echo "eth0:unavailable" ;;
  "-t -f NAME,TYPE connection show") cat "$FAKE/conns" ;;
esac
exit 0
''',
    "wifi-connect": '#!/bin/bash\necho "$*" >> "$FAKE/ap.log"\n',
}

KNOWN = "preconfigured:802-11-wireless\nWired connection 1:802-3-ethernet\n"


@pytest.fixture
def wp(tmp_path):
    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    for name, body in FAKES.items():
        f = fake / "bin" / name
        f.write_text(body)
        f.chmod(0o755)
    (fake / "state").write_text("connected")
    (fake / "conns").write_text(KNOWN)

    class WP:
        def set(self, state=None, conns=None):
            if state is not None:
                (fake / "state").write_text(state)
            if conns is not None:
                (fake / "conns").write_text(conns)

        def run(self, steps, grace=600):
            """Corre step() en cada tiempo de `steps` en un mismo proceso (el estado del script vive
            entre pasos, no entre llamadas); un elemento (t, state) cambia el estado antes."""
            calls = []
            for s in steps:
                if isinstance(s, tuple):
                    t, state = s
                    calls.append(f"echo {shlex.quote(state)} > \"$FAKE/state\"")
                else:
                    t = s
                calls.append(f"step {t}")
            env = dict(os.environ, FAKE=str(fake), PATH=f"{fake / 'bin'}:{os.environ['PATH']}",
                       WP_GRACE_S=str(grace))
            r = subprocess.run(["bash", "-c", f"source '{SCRIPT}'; " + "; ".join(calls)],
                               env=env, capture_output=True, text=True, timeout=10)
            assert r.returncode == 0, r.stderr
            return r.stdout

        @property
        def ap_runs(self):
            f = fake / "ap.log"
            return f.read_text().splitlines() if f.exists() else []

        @property
        def nmcli_log(self):
            return (fake / "nmcli.log").read_text()

    return WP()


def test_connected_never_raises_the_ap(wp):
    wp.run([0, 30, 600, 1200, 5000])
    assert wp.ap_runs == []


def test_short_wifi_blip_does_not_raise_the_ap(wp):
    # Lo que tiraba a dev2: unos segundos sin red y vuelve.
    wp.run([0, (30, "disconnected"), 60, (90, "connected"), 700, 1300])
    assert wp.ap_runs == []


def test_connecting_state_counts_as_no_network_but_within_grace(wp):
    wp.run([(0, "connecting (getting IP configuration)"), 300, (590, "connected")])
    assert wp.ap_runs == []


def test_down_for_the_whole_grace_raises_the_ap_once(wp):
    out = wp.run([(0, "disconnected"), 300, 599, 600, 630])
    assert len(wp.ap_runs) == 1
    assert "--portal-ssid SfyBench" in wp.ap_runs[0]
    assert "--activity-timeout 300" in wp.ap_runs[0]
    assert "device connect wlan0" in wp.nmcli_log
    assert "hace 600s" in out


@pytest.mark.parametrize("last, aps", [(1159, 0), (1160, 1)])
def test_grace_restarts_after_reconnecting(wp, last, aps):
    # Sin red a los 0, vuelve a los 550: la cuenta arranca de nuevo a los 560.
    wp.run([(0, "disconnected"), 500, (550, "connected"), (560, "disconnected"), 1100, last])
    assert len(wp.ap_runs) == aps


@pytest.mark.parametrize("last, aps", [(1229, 1), (1230, 2)])
def test_after_the_portal_waits_a_full_grace_again(wp, last, aps):
    # AP a los 600; la próxima cuenta arranca en el chequeo siguiente (630).
    wp.run([(0, "disconnected"), 600, 630, last])
    assert len(wp.ap_runs) == aps


def test_no_saved_wifi_raises_the_ap_right_away(wp):
    # Pi nueva: no hay nada que esperar.
    wp.set(state="disconnected", conns="Wired connection 1:802-3-ethernet\n")
    wp.run([0])
    assert len(wp.ap_runs) == 1


def test_own_ap_profile_is_not_a_saved_network(wp):
    wp.set(state="disconnected", conns="SfyBench:802-11-wireless\nSfyBench 1:802-11-wireless\n")
    wp.run([0])
    assert len(wp.ap_runs) == 1


def test_sourcing_does_not_start_the_loop(wp):
    # Si sourcear corriera main(), el subprocess colgaría (sleep 20 + loop) y vencería el timeout.
    assert wp.run([]) == ""

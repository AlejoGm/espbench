"""bench-master corriendo en un bench (remote/infra/espbench-master + master --allow-host): se activa a mano,
escucha en la IP de Tailscale y acepta el nombre del bench. Un bench que no lo activó no cambia."""
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SCRIPT = ROOT / "remote/infra/espbench-master"


def test_allowed_hosts_adds_the_bench_names():
    pytest.importorskip("uvicorn")
    from master.__main__ import allowed_hosts
    assert allowed_hosts("100.64.0.10", ["sensibox,Sensibox.tailnet-ejemplo.ts.net"]) == {
        "localhost", "127.0.0.1", "::1", "100.64.0.10", "sensibox", "sensibox.tailnet-ejemplo.ts.net"}
    assert allowed_hosts("127.0.0.1") == {"localhost", "127.0.0.1", "::1"}
    assert allowed_hosts("0.0.0.0", ["sensibox"]) is None


def test_guard_accepts_the_bench_name_and_still_blocks_cross_site():
    httpx = pytest.importorskip("httpx")
    import asyncio
    from master.app import create_app
    from tests.test_master import _cache
    from master.__main__ import allowed_hosts
    app = create_app(cache=_cache(), poll_s=0, allowed_hosts=allowed_hosts("100.64.0.10", ["sensibox"]))

    async def go():
        t = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=t, base_url="http://sensibox:8090") as c:
            ok = await c.get("/api/version")
            csrf = await c.post("/api/rescan", headers={"Origin": "https://evil.example"})
            same = await c.post("/api/rescan", headers={"Origin": "http://sensibox:8090"})
        async with httpx.AsyncClient(transport=t, base_url="http://192.168.1.50:8090") as c:
            lan = await c.get("/api/version")
        return ok, csrf, same, lan
    ok, csrf, same, lan = asyncio.run(go())
    assert ok.status_code == 200 and same.status_code == 200
    assert csrf.status_code == 403 and lan.status_code == 403        # otro nombre (la IP de la red local): no


FAKES = {
    "tailscale": r'''#!/bin/bash
case "$*" in
  "ip -4") [ -f "$FAKE/no_ip" ] || echo 100.64.0.10 ;;
  "status --json") echo '{"Self": {"DNSName": "sensibox.tailnet-ejemplo.ts.net."}}' ;;
esac
''',
    "systemctl": '#!/bin/bash\necho "systemctl $*" >> "$FAKE/calls"\n[ "$1" = is-active ] && echo active\nexit 0\n',
    "hostname": "#!/bin/bash\necho sensibox\n",
    "python3": r'''#!/bin/bash
# python3 -m venv DIR → arma un venv falso (pip y python que anotan lo que les piden); el resto, el python real
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  printf '#!/bin/bash\necho "pip $*" >> "%s/calls"\n' "$FAKE" > "$3/bin/pip"
  printf '#!/bin/bash\necho "python $* (en $PWD) cfg=$ESPBENCH_BENCHES_CONFIG" >> "%s/calls"\n' "$FAKE" > "$3/bin/python"
  chmod +x "$3/bin/pip" "$3/bin/python"
  exit 0
fi
exec "$REAL_PYTHON" "$@"
''',
}


@pytest.fixture
def bench(tmp_path):
    fake, base, repo = tmp_path / "fake", tmp_path / "esp", tmp_path / "repo"
    for d in (fake / "bin", base, repo / "master"):
        d.mkdir(parents=True)
    (repo / "master/requirements.txt").write_text("fastapi\n")
    (base / "update.conf").write_text(f"REPO_DIR={repo}\nPIN=\n")
    for name, body in FAKES.items():
        (fake / "bin" / name).write_text(body)
        (fake / "bin" / name).chmod(0o755)
    env = {"PATH": f"{fake / 'bin'}:/usr/bin:/bin", "ESP_BASE": str(base), "ESPBENCH_MASTER_TEST": "1",
           "FAKE": str(fake), "REAL_PYTHON": sys.executable}

    class B:
        def run(self, *args, check=True):
            r = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)
            if check:
                assert r.returncode == 0, r.stderr
            return r

        def calls(self):
            p = fake / "calls"
            return p.read_text().splitlines() if p.exists() else []
    b = B()
    b.fake, b.base, b.repo = fake, base, repo
    return b


def test_not_enabled_by_default(bench):
    assert "no está activado" in bench.run("status").stdout
    r = bench.run("run", check=False)
    assert r.returncode == 1 and "no está activado" in r.stderr
    assert bench.calls() == []


def test_enable_run_disable(bench):
    out = bench.run("enable").stdout
    conf = (bench.base / "bench-master.conf").read_text()
    assert conf == "PORT=8090\nALLOW_HOSTS=sensibox,sensibox.tailnet-ejemplo.ts.net\nHOST=\n"
    assert "http://sensibox:8090/" in out
    assert f"pip install -q -r {bench.repo}/master/requirements.txt" in bench.calls()
    assert "systemctl enable --now bench-master" in bench.calls()
    bench.run("run")
    assert bench.calls()[-1] == (f"python -m master --host 100.64.0.10 --port 8090 "
                                 f"--allow-host sensibox,sensibox.tailnet-ejemplo.ts.net (en {bench.repo}) "
                                 f"cfg={bench.base}/meta/master-benches.json")
    # este bench en la lista de bench-master (Tailscale no se lista a sí mismo)
    assert json.loads((bench.base / "meta/master-benches.json").read_text()) == {
        "tailscale": True, "hosts": ["100.64.0.10:8080"]}
    assert "100.64.0.10" in bench.run("status").stdout
    bench.run("disable")
    assert not (bench.base / "bench-master.conf").exists()
    assert "systemctl disable --now bench-master" in bench.calls()


def test_run_waits_for_tailscale_and_enable_needs_it(bench):
    bench.run("enable")
    (bench.fake / "no_ip").touch()
    r = bench.run("run", check=False)
    assert r.returncode == 1 and "Tailscale" in r.stderr                    # systemd reintenta
    (bench.base / "bench-master.conf").unlink()
    r = bench.run("enable", check=False)
    assert r.returncode == 1 and "Tailscale no tiene IP" in r.stderr


def test_service_only_runs_when_enabled():
    unit = (ROOT / "remote/infra/bench-master.service").read_text()
    assert "ConditionPathExists=/opt/esp/bench-master.conf" in unit
    assert "User=sfypi" in unit and "ExecStart=/usr/local/bin/espbench-master run" in unit


def test_update_restarts_it_only_if_running():
    upd = (ROOT / "remote/infra/espbench-update").read_text()
    assert "systemctl try-restart bench-master" in upd

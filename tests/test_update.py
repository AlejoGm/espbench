"""espbench-update (remote/infra/espbench-update) con git de verdad: un origin bare con
releases y ramas, y el clone del bench. install.sh es uno falso de cada commit (copia
VERSION a ESP_BASE; si el commit tiene BROKEN, el dashboard "no levanta"). systemctl,
devremote y curl son fakes en el PATH."""
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).parent.parent
SCRIPT = ROOT / "remote" / "infra" / "espbench-update"

FAKE_INSTALL = r'''#!/bin/bash
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cp "$REPO/VERSION" "$ESP_BASE/VERSION"
echo "install $(cat "$REPO/VERSION")" >> "$FAKE/install.log"
if [ -f "$REPO/BROKEN" ]; then touch "$FAKE/down"; else rm -f "$FAKE/down"; fi
'''

FAKES = {
    "systemctl": '#!/bin/bash\necho "systemctl $*" >> "$FAKE/calls.log"\n',
    "devremote": '#!/bin/bash\necho "devremote $*" >> "$FAKE/calls.log"\n',
    "sleep": "#!/bin/bash\nexit 0\n",
    # /api/version del dashboard: la versión instalada, salvo que el último install lo haya roto
    "curl": '#!/bin/bash\n[ -f "$FAKE/down" ] && exit 7\nprintf \'{"version": "%s"}\' "$(cat "$ESP_BASE/VERSION")"\n',
}

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env={**os.environ, **GIT_ENV})
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class Bench:
    def __init__(self, tmp):
        self.tmp = tmp
        self.origin, self.dev, self.repo = tmp / "origin.git", tmp / "dev", tmp / "bench-clone"
        self.base, self.fake = tmp / "esp", tmp / "fake"
        (self.fake / "bin").mkdir(parents=True)
        for name, body in FAKES.items():
            f = self.fake / "bin" / name
            f.write_text(body)
            f.chmod(0o755)
        (self.base / "run").mkdir(parents=True)
        (self.base / "locks").mkdir()
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.dev)], check=True)
        git(self.dev, "remote", "add", "origin", str(self.origin))
        (self.dev / "remote").mkdir()
        inst = self.dev / "remote" / "install.sh"
        inst.write_text(FAKE_INSTALL)
        inst.chmod(0o755)
        self.commit("0.1.0")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True,
                       env={**os.environ, **GIT_ENV})
        # instalado: lo que tiene el clone
        (self.base / "VERSION").write_text("0.1.0\n")
        self.conf(PIN="")
        self.env = {**os.environ, **GIT_ENV, "PATH": f"{self.fake / 'bin'}:{os.environ['PATH']}",
                    "ESP_BASE": str(self.base), "FAKE": str(self.fake), "ESPBENCH_PYTHON": sys.executable,
                    "ESPBENCH_PYTHONPATH": str(ROOT / "remote"), "ESPBENCH_HEALTH_TIMEOUT": "4",
                    "TMPDIR": str(tmp)}

    def commit(self, version, branch="main", tag=None, broken=False):
        if git(self.dev, "branch", "--show-current") != branch:
            git(self.dev, "checkout", "-q", "-B", branch)
        (self.dev / "VERSION").write_text(version + "\n")
        b = self.dev / "BROKEN"
        if broken:
            b.write_text("x")
        elif b.exists():
            b.unlink()
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", version)
        if tag:
            git(self.dev, "tag", tag)
        git(self.dev, "push", "-q", "--tags", "origin", branch)
        return git(self.dev, "rev-parse", "HEAD")

    def conf(self, **kw):
        lines = [f"REPO_DIR={self.repo}"] + [f"{k}={v}" for k, v in kw.items()]
        (self.base / "update.conf").write_text("\n".join(lines) + "\n")

    def pin(self):
        pins = [l[4:] for l in (self.base / "update.conf").read_text().splitlines() if l.startswith("PIN=")]
        return pins[-1] if pins else None

    def run(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *args], env=self.env, capture_output=True, text=True)

    def status(self):
        return json.loads((self.base / "update_status.json").read_text())

    def head(self):
        return git(self.repo, "rev-parse", "HEAD")

    def installed(self):
        return (self.base / "VERSION").read_text().strip()

    def calls(self):
        f = self.fake / "calls.log"
        return f.read_text().splitlines() if f.exists() else []


@pytest.fixture
def bench(tmp_path):
    return Bench(tmp_path)


def test_auto_goes_to_latest_release_by_version_not_date(bench):
    newest = bench.commit("0.10.0", tag="v0.10.0")
    bench.commit("0.2.0", tag="v0.2.0")            # más nuevo en el tiempo, menor en versión
    bench.commit("0.11.0-dev")                     # main sin taggear: no es release
    r = bench.run("--auto")
    assert r.returncode == 0, r.stdout + r.stderr
    assert bench.head() == newest and bench.installed() == "0.10.0"
    st = bench.status()
    assert st["state"] == "ok" and st["target"] == "v0.10.0" and st["mode"] == "auto"
    assert st["from"]["version"] == "0.1.0" and st["to"]["version"] == "0.10.0" and st["pin"] is None
    assert "systemctl restart dashboard" in bench.calls() and "devremote --reset" in bench.calls()
    assert bench.pin() == ""


def test_auto_respects_pin(bench):
    bench.commit("0.2.0", tag="v0.2.0")
    bench.conf(PIN="feat/x")
    r = bench.run("--auto")
    assert r.returncode == 0 and bench.status()["state"] == "skipped" and "feat/x" in bench.status()["message"]
    assert bench.installed() == "0.1.0" and bench.calls() == []


def test_auto_skips_busy_bench(bench):
    bench.commit("0.2.0", tag="v0.2.0")
    run_file = bench.base / "run" / "ttyUSB0.json"
    run_file.write_text(json.dumps({"state": "flashing", "pid": os.getpid()}))
    r = bench.run("--auto")
    assert r.returncode == 0 and bench.status()["state"] == "skipped"
    assert "ttyUSB0 flashing" in bench.status()["message"] and bench.installed() == "0.1.0"

    run_file.write_text(json.dumps({"state": "monitoring", "pid": os.getpid()}))
    (bench.base / "locks" / "ttyUSB0").write_text(f"juan:t0k:{int(time.time()) + 600}")
    bench.run("--auto")
    assert "reservada por juan" in bench.status()["message"] and bench.installed() == "0.1.0"

    (bench.base / "locks" / "ttyUSB0").write_text("juan:t0k")      # lock del flash: no es reserva
    bench.run("--auto")
    assert bench.status()["state"] == "ok" and bench.installed() == "0.2.0"


def test_manual_busy_fails_unless_force(bench):
    bench.commit("0.2.0", tag="v0.2.0")
    (bench.base / "run" / "esp-slot1.json").write_text(json.dumps({"state": "erasing", "pid": os.getpid()}))
    r = bench.run("--release")
    assert r.returncode == 1 and bench.status()["state"] == "failed" and "--force" in bench.status()["message"]
    assert bench.run("--release", "--force").returncode == 0 and bench.installed() == "0.2.0"


def test_ref_pins_a_branch_and_follows_it(bench):
    bench.commit("0.2.0", tag="v0.2.0")
    first = bench.commit("0.3.0-a", branch="feat/x")
    r = bench.run("--ref", "feat/x")
    assert r.returncode == 0, r.stdout + r.stderr
    assert bench.head() == first and bench.installed() == "0.3.0-a" and bench.pin() == "feat/x"
    assert bench.status()["pin"] == "feat/x"

    second = bench.commit("0.3.0-b", branch="feat/x")
    assert bench.run().returncode == 0          # sin args: lo último del PIN
    assert bench.head() == second and bench.installed() == "0.3.0-b"

    assert bench.run("--auto").returncode == 0 and bench.status()["state"] == "skipped"

    assert bench.run("--release").returncode == 0
    assert bench.installed() == "0.2.0" and bench.pin() == ""


def test_ref_can_be_a_tag_or_commit(bench):
    sha = bench.commit("0.2.0")
    bench.commit("0.3.0", tag="v0.3.0")
    assert bench.run("--ref", sha[:10]).returncode == 0 and bench.installed() == "0.2.0"
    assert bench.run("--ref", "v0.3.0").returncode == 0 and bench.installed() == "0.3.0"


def test_broken_release_rolls_back(bench):
    good = bench.commit("0.2.0", tag="v0.2.0")
    assert bench.run("--auto").returncode == 0
    bench.commit("0.3.0", tag="v0.3.0", broken=True)
    r = bench.run("--auto")
    assert r.returncode == 1
    st = bench.status()
    assert st["state"] == "rolled_back" and "v0.3.0" in st["message"]
    assert bench.head() == good and bench.installed() == "0.2.0"
    installs = (bench.fake / "install.log").read_text().splitlines()
    assert installs[-2:] == ["install 0.3.0", "install 0.2.0"]


def test_up_to_date_does_nothing(bench):
    bench.commit("0.2.0", tag="v0.2.0")
    bench.run("--auto")
    calls = len(bench.calls())
    assert bench.run("--auto").returncode == 0
    assert bench.status()["state"] == "up_to_date" and len(bench.calls()) == calls


def test_no_releases_and_unknown_ref(bench):
    assert bench.run("--auto").returncode == 0 and bench.status()["state"] == "skipped"
    assert "no hay releases" in bench.status()["message"]
    r = bench.run("--ref", "no-existe")
    assert r.returncode == 1 and bench.status()["state"] == "failed" and bench.pin() == ""


def test_fetch_failure(bench):
    bench.commit("0.2.0", tag="v0.2.0")
    git(bench.repo, "remote", "set-url", "origin", str(bench.tmp / "no-existe.git"))
    assert bench.run("--auto").returncode == 0 and bench.status()["state"] == "skipped"
    assert bench.run("--release").returncode == 1 and bench.status()["state"] == "failed"


def test_one_update_at_a_time(bench):
    lock = bench.base / "update.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))         # otro update vivo
    r = bench.run("--auto")
    assert r.returncode == 75 and "corriendo" in r.stderr
    (lock / "pid").write_text("999999")                 # murió: se toma el lock
    assert bench.run("--auto").returncode == 0 and not lock.exists()


def test_bad_args(bench):
    assert bench.run("--ref", "--release").returncode == 2
    assert bench.run("--nada").returncode == 2


def test_missing_repo(bench):
    (bench.base / "update.conf").write_text("PIN=\n")
    r = bench.run("--auto")
    assert r.returncode == 1 and "REPO_DIR" in bench.status()["message"]


# ---------- --setup (lo llama install.sh) ----------

def conf_lines(bench):
    return (bench.base / "update.conf").read_text().splitlines()


def test_setup_on_empty_conf_pins_the_branch(bench):
    """El bug de sensipi04: update.conf vacío (touch) y un grep sin match abortaba el install."""
    (bench.base / "update.conf").write_text("")
    git(bench.repo, "checkout", "-q", "-b", "feat/x")
    r = bench.run("--setup", str(bench.repo))
    assert r.returncode == 0, r.stderr
    assert conf_lines(bench) == [f"REPO_DIR={bench.repo}", "PIN=feat/x"]
    assert f"REPO_DIR={bench.repo}" in r.stdout


def test_setup_without_conf_on_a_release_follows_releases(bench):
    (bench.base / "update.conf").unlink()
    bench.commit("0.2.0", tag="v0.2.0")
    git(bench.repo, "fetch", "-q", "--tags")
    git(bench.repo, "checkout", "-q", "--detach", "v0.2.0")
    assert bench.run("--setup", str(bench.repo)).returncode == 0
    assert conf_lines(bench) == [f"REPO_DIR={bench.repo}", "PIN="]


def test_setup_detached_commit_pins_the_commit(bench):
    (bench.base / "update.conf").unlink()
    git(bench.repo, "checkout", "-q", "--detach", "HEAD")
    bench.run("--setup", str(bench.repo))
    assert conf_lines(bench)[1] == "PIN=" + git(bench.repo, "rev-parse", "--short", "HEAD")


def test_setup_keeps_pin_and_moves_repo_dir(bench):
    (bench.base / "update.conf").write_text("REPO_DIR=/viejo\nPIN=\n")
    bench.run("--setup", str(bench.repo))       # en main, pero ya había PIN (vacío): no se toca
    assert sorted(conf_lines(bench)) == sorted([f"REPO_DIR={bench.repo}", "PIN="])


def test_setup_not_a_repo(bench):
    r = bench.run("--setup", str(bench.tmp))
    assert r.returncode == 1 and "no es un repo" in r.stderr

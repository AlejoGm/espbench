"""deploy.py después de mover el camino del flash a client/espbench_lib.py:
importa de la lib y su salida para humanos no cambia."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client import deploy, espbench_lib  # noqa: E402
from tests.benchsim import Bench  # noqa: E402
from tests.test_espbench_lib import make_build  # noqa: E402


def test_deploy_uses_the_lib():
    assert deploy._lib is espbench_lib
    assert deploy._hw_model_from_build is espbench_lib.hw_model_from_build
    import inspect
    src = inspect.getsource(deploy.flash_one)               # wrapper con los prints de siempre
    assert "_lib.flash_one(" in src and "create_connection" not in src


def test_collect_artifact_normal_mode_goes_through_the_lib(tmp_path, monkeypatch, capsys):
    calls = []
    real = espbench_lib.collect_artifact

    def spy(build_dir, include_elf=True, log=None):
        calls.append((pathlib.Path(build_dir), include_elf))
        return real(build_dir, include_elf=include_elf, log=log)
    monkeypatch.setattr(espbench_lib, "collect_artifact", spy)
    build = make_build(tmp_path)
    deploy.collect_artifact(build, is_remote=True)
    assert calls == [(build, True)]
    out = capsys.readouterr().out
    assert "[ARTIFACT] Recolectando archivos para flashear..." in out
    assert "[ARTIFACT] + simfw.bin (64 bytes)" in out and "[ARTIFACT] + firmware.elf (3 bytes)" in out
    assert "[ARTIFACT] ✓ Artifact creado: artifact.zip" in out


def test_collect_artifact_without_build_is_systemexit(tmp_path):
    try:
        deploy.collect_artifact(tmp_path / "no-build", is_remote=True)
    except SystemExit as e:
        assert "corré un build primero" in str(e)
    else:
        raise AssertionError("tenía que salir")


def test_custom_mode_still_uses_its_own_flasher_args(tmp_path):
    build = make_build(tmp_path)
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "fa.json").write_text(json.dumps({"flash_files": {"0x10000": "otro.bin"}}))
    (custom / "otro.bin").write_bytes(b"x")
    import zipfile
    art = deploy.collect_artifact(build, custom_flasher_args_path=str(custom / "fa.json"), is_custom_mode=True,
                                  is_remote=True)
    assert sorted(zipfile.ZipFile(art).namelist()) == ["flasher_args.json", "otro.bin"]


def test_deploy_flash_one_against_the_real_protocol(tmp_path, capsys):
    """flash_one de deploy (wrapper de la lib) contra protocol.serve_connection
    real: verbose imprime las fases y el log de esptool como antes."""
    with Bench() as bench:
        board = bench.add_board(key="sim-board")
        art = espbench_lib.collect_artifact(make_build(tmp_path))
        remote = {"name": "sim-board", "host": "127.0.0.1", "port": board.port, "token": "",
                  "lock_user": "alejo", "lock_token": "t0k"}
        lines = []
        r = deploy.flash_one(remote, art, espbench_lib.sha256_file(art), art.stat().st_size, "job_20261006_120000",
                             "esp32", 921600, False, False, on_line=lines.append, verbose=True)
    assert r["ok"] and r["status"] == "exitoso" and r["name"] == "sim-board"
    assert "Hard resetting via RTS pin..." in lines
    out = capsys.readouterr().out
    assert "  conectando..." in out and "  flasheando..." in out and "Writing at 0x00010000... (100 %)" in out
    assert r["logs"][0] == "  conectando..."


def test_client_works_without_fcntl(tmp_path, monkeypatch, capsys):
    """Windows no tiene fcntl: deploy.py y la lib tienen que importar igual, y
    el CLI anda sin el flock del registro local de reservas (antes, `import
    fcntl` a nivel de módulo rompía deploy.py en Windows)."""
    import importlib
    import client as client_pkg
    with Bench() as bench:
        bench.add_board(key="sim-board")
        for name in ("espbench_lib", "deploy", "espbench"):
            monkeypatch.delitem(sys.modules, f"client.{name}", raising=False)
            if hasattr(client_pkg, name):
                monkeypatch.setattr(client_pkg, name, getattr(client_pkg, name))
        monkeypatch.setitem(sys.modules, "fcntl", None)        # import fcntl → ImportError
        lib = importlib.import_module("client.espbench_lib")
        importlib.import_module("client.deploy")
        cli = importlib.import_module("client.espbench")
        assert lib.fcntl is None
        for k, v in {"ESPBENCH_HOST": bench.host, "ESPBENCH_USER": "win", "ESPBENCH_LOCK_TOKEN": "t0k",
                     "ESPBENCH_CONFIG": str(tmp_path / "no.json"), "ESPBENCH_STATE_DIR": str(tmp_path / "st")}.items():
            monkeypatch.setenv(k, v)
        monkeypatch.chdir(tmp_path)
        capsys.readouterr()
        assert cli.main(["reserve", "sim-board", "--json"]) == 0
        assert "win" in json.dumps(json.loads((tmp_path / "st" / "reservations.json").read_text()))
        assert cli.main(["release", "sim-board", "--json"]) == 0
        assert json.loads((tmp_path / "st" / "reservations.json").read_text()) == {}

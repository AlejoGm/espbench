import pytest, pathlib, tempfile, json, zipfile, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))
from client.deploy import collect_artifact

def make_build_dir(include_elf=False):
    tmp = pathlib.Path(tempfile.mkdtemp())
    fa = {"flash_files": {"0x1000": "bootloader.bin", "0x10000": "app.bin"}}
    (tmp / "flasher_args.json").write_text(json.dumps(fa))
    (tmp / "bootloader.bin").write_bytes(b"fake boot")
    (tmp / "app.bin").write_bytes(b"fake app")
    if include_elf:
        (tmp / "myapp.elf").write_bytes(b"fake elf")
    return tmp

def test_remote_includes_elf():
    d = make_build_dir(include_elf=True)
    artifact = collect_artifact(d, is_remote=True)
    with zipfile.ZipFile(artifact) as z:
        assert "firmware.elf" in z.namelist()

def test_local_excludes_elf():
    d = make_build_dir(include_elf=True)
    artifact = collect_artifact(d, is_remote=False)
    with zipfile.ZipFile(artifact) as z:
        assert "firmware.elf" not in z.namelist()

def test_remote_no_elf_no_error():
    d = make_build_dir(include_elf=False)
    artifact = collect_artifact(d, is_remote=True)  # should not raise
    with zipfile.ZipFile(artifact) as z:
        assert "firmware.elf" not in z.namelist()

"""board_meta: catálogo de tags (tags.json), validación de nota/tags; DevicesFile.set_meta."""
import datetime as dt
import json
import multiprocessing
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server import board_meta, paths  # noqa: E402
from server.board_meta import MetaError  # noqa: E402
from server.device_registry import DevicesFile  # noqa: E402

MAC = "AA:BB:CC:DD:EE:FF"


def test_repo_catalog_is_valid_and_has_the_warn_tags():
    raw = json.loads(board_meta.CATALOG_FILE.read_text())["tags"]
    cat = board_meta.catalog()
    assert len(cat) == len(raw)                 # ninguna entrada del repo se descarta por inválida
    ids = [t["id"] for t in cat]
    assert {"no-tocar", "roto", "agentes", "lte", "esp32-s3", "modbus"} <= set(ids)
    assert set(board_meta.warn_ids(cat)) == {"no-tocar", "roto"}
    assert all(t["label"] and t["group"] and t["desc"] for t in cat)


def test_catalog_skips_invalid_entries(tmp_path):
    f = tmp_path / "tags.json"
    f.write_text(json.dumps({"tags": [{"id": "ok", "group": "g"}, {"id": "Mal"}, {"id": "ok"}, "x",
                                      {"id": "a" * 25}, {"id": "w", "kind": "warn", "color": "#f00"}]}))
    assert board_meta.catalog(f) == [{"id": "ok", "label": "ok", "group": "g", "desc": ""},
                                     {"id": "w", "label": "w", "group": "otros", "desc": "", "kind": "warn",
                                      "color": "#f00"}]
    f.write_text("{roto")
    assert board_meta.catalog(f) == []


@pytest.mark.parametrize("text,out", [(None, ""), ("", ""), ("  testeando, no tocar  ", "testeando, no tocar"),
                                      ("dev alejo · ñandú", "dev alejo · ñandú"), ("x" * 200, "x" * 200)])
def test_clean_note(text, out):
    assert board_meta.clean_note(text) == out


@pytest.mark.parametrize("bad", ["x" * 201, "a\nb", "a\tb", "a\x1bb", "a\x85b", 3])
def test_clean_note_rejects(bad):
    with pytest.raises(MetaError):
        board_meta.clean_note(bad)


def test_normalize_and_check_tags():
    assert board_meta.normalize_tags([" LTE", "lte", "Modbus", ""]) == ["lte", "modbus"]
    for bad in ("lte", [1], {"a": 1}):
        with pytest.raises(MetaError):
            board_meta.normalize_tags(bad)
    cat = board_meta.catalog()
    board_meta.check_known(["lte", "no-tocar"], cat)
    with pytest.raises(MetaError, match=r"'modbsu' \(¿'modbus'\?\).*Válidos: no-tocar"):
        board_meta.check_known(["modbsu"], cat)


def test_merge_tags_limit():
    assert board_meta.merge_tags(["a"], None, ["b", "a"], ["a"]) == ["b"]
    assert board_meta.merge_tags(["a"], ["c"], ["d"], []) == ["c", "d"]
    with pytest.raises(MetaError, match="12"):
        board_meta.merge_tags([], [str(i) for i in range(13)], [], [])


def test_set_meta_note_and_tags():
    f = DevicesFile()
    f.update_device_key(MAC, "mi-placa")
    now = dt.datetime(2026, 10, 6, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=-3)))
    r = f.set_meta(MAC, "alejo", note="testeando", tags_add=["lte", "modbus"], now=now)
    assert r["note_changed"] and r["added"] == ["lte", "modbus"] and r["removed"] == []
    e = f.get_all()[MAC]
    assert (e["device_key"], e["note"], e["note_by"], e["note_at"], e["tags"]) == (
        "mi-placa", "testeando", "alejo", "2026-10-06T10:00:00-03:00", ["lte", "modbus"])
    r = f.set_meta(MAC, "otro", note="testeando", tags_remove=["lte", "nada"])
    assert not r["note_changed"] and r["removed"] == ["lte"]
    assert f.get_all()[MAC]["note_by"] == "alejo"           # misma nota: no cambia quién ni cuándo
    r = f.set_meta(MAC, "alejo", note="", tags=[])
    assert r["note_changed"] and r["removed"] == ["modbus"]
    assert f.get_all()[MAC] == {"device_key": "mi-placa", "hw_model": None}
    with pytest.raises(KeyError):
        f.set_meta("11:22:33:44:55:66", "x", note="hola")


def test_set_meta_over_the_limit_does_not_write():
    f = DevicesFile()
    f.update_device_key(MAC, "p")
    before = paths.devices_file().read_text()
    with pytest.raises(MetaError):
        f.set_meta(MAC, "x", tags=[f"t{i}" for i in range(13)])
    assert paths.devices_file().read_text() == before


def _add_tag(i):
    DevicesFile().set_meta(MAC, f"u{i}", tags_add=[f"t{i}"])


def test_set_meta_from_two_processes_keeps_both():
    """Cada set_meta es un leer-modificar-escribir con el flock: dos procesos no se pisan."""
    DevicesFile().update_device_key(MAC, "p")
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_add_tag, args=(i,)) for i in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    assert sorted(DevicesFile().get_all()[MAC]["tags"]) == sorted(f"t{i}" for i in range(8))

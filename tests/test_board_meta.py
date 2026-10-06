"""board_meta: nota, propiedades (categorías fijas, valores por bench en properties.json),
DevicesFile.set_meta."""
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


def cat_by_id():
    return {c["id"]: c for c in board_meta.catalog()}


def test_initial_catalog():
    cats = cat_by_id()
    assert list(cats) == ["estado", "uso", "chip", "conectividad", "perifericos"]
    assert [c["multi"] for c in cats.values()] == [False, True, False, True, True]
    assert [v["id"] for v in cats["chip"]["values"]] == ["esp32", "esp32-s3", "esp32-c3"]
    assert board_meta.excluded_values(board_meta.catalog()) == {"estado": ["no-tocar", "roto"]}
    assert not paths.properties_file().exists()          # leer no escribe


def test_add_and_remove_values():
    e = board_meta.add_value("conectividad", " NB-IoT ".strip().lower().replace(" ", ""), desc="NB-IoT")
    assert e == {"id": "nb-iot", "label": "nb-iot", "desc": "NB-IoT"}
    board_meta.add_value("estado", "prestada", label="prestada", warn=True, exclude_pick=True)
    cats = cat_by_id()
    assert cats["conectividad"]["values"][-1]["id"] == "nb-iot"
    assert cats["estado"]["values"][-1] == {"id": "prestada", "label": "prestada", "desc": "", "warn": True,
                                            "exclude_pick": True}
    assert json.loads(paths.properties_file().read_text())["values"]["chip"][0]["id"] == "esp32"
    for args, kw, msg in [(("conectividad", "lte"), {}, "ya existe"), (("nada", "x"), {}, "categoría"),
                          (("chip", "Mal Valor"), {}, "inválido"), (("chip", "x"), {"warn": True}, "solo en"),
                          (("chip", "x"), {"desc": "a\nb"}, "control")]:
        with pytest.raises(MetaError, match=msg):
            board_meta.add_value(*args, **kw)
    with pytest.raises(board_meta.InUseError, match="placa-1"):
        board_meta.remove_value("conectividad", "nb-iot", lambda c, v: ["placa-1"])
    board_meta.remove_value("conectividad", "nb-iot", lambda c, v: [])
    assert "nb-iot" not in [v["id"] for v in cat_by_id()["conectividad"]["values"]]
    with pytest.raises(board_meta.NotFoundError):
        board_meta.remove_value("conectividad", "nb-iot", lambda c, v: [])


def test_broken_catalog_file_falls_back_to_seed_and_new_category():
    paths.properties_file().parent.mkdir(parents=True, exist_ok=True)
    paths.properties_file().write_text("{roto")
    assert len(cat_by_id()["chip"]["values"]) == 3
    paths.properties_file().write_text(json.dumps({"values": {"chip": [{"id": "esp32-p4"}, {"id": "MAL"}]}}))
    cats = cat_by_id()
    assert [v["id"] for v in cats["chip"]["values"]] == ["esp32-p4"]
    assert len(cats["uso"]["values"]) == 3                # categoría ausente del archivo: su set inicial


@pytest.mark.parametrize("text,out", [(None, ""), ("", ""), ("  testeando, no tocar  ", "testeando, no tocar"),
                                      ("dev alejo · ñandú", "dev alejo · ñandú"), ("x" * 200, "x" * 200)])
def test_clean_note(text, out):
    assert board_meta.clean_note(text) == out


@pytest.mark.parametrize("bad", ["x" * 201, "a\nb", "a\tb", "a\x1bb", "a\x85b", 3])
def test_clean_note_rejects(bad):
    with pytest.raises(MetaError):
        board_meta.clean_note(bad)


def test_plan_and_apply_props():
    ops = board_meta.plan_props({"chip": "ESP32-S3", "estado": None}, {"conectividad": ["lte", "wifi"]},
                                {"uso": "ci"})
    assert ops == {"chip": ("set", ["esp32-s3"]), "estado": ("set", []), "conectividad": ("add", ["lte", "wifi"]),
                   "uso": ("remove", ["ci"])}
    cur = {"estado": "testeando", "uso": ["ci", "demo"], "conectividad": ["wifi"]}
    new, changes = board_meta.apply_props(cur, ops)
    assert new == {"chip": "esp32-s3", "uso": ["demo"], "conectividad": ["wifi", "lte"]}
    assert changes == {"chip": {"from": None, "to": "esp32-s3"}, "estado": {"from": "testeando", "to": None},
                       "conectividad": {"from": ["wifi"], "to": ["wifi", "lte"]},
                       "uso": {"from": ["ci", "demo"], "to": ["demo"]}}
    assert board_meta.apply_props(new, board_meta.plan_props(add={"chip": "esp32"}))[0]["chip"] == "esp32"
    assert board_meta.apply_props(new, board_meta.plan_props(add={"uso": "demo"}))[1] == {}


@pytest.mark.parametrize("args,msg", [
    (({"chip": "esp32-s4"},), r"'esp32-s4' no es un valor válido \(¿'esp32-s3'\?\)"),
    (({"color": "rojo"},), "categoría"), (({"chip": ["esp32", "esp32-c3"]},), "un solo valor"),
    ((None, {"chip": ["esp32", "esp32-c3"]}), "un solo valor"), (({"chip": 3},), "valor"),
    (("chip=esp32",), "categoría: valor"), (({"uso": "ci"}, {"uso": "demo"}), "dos veces"),
])
def test_plan_props_rejects(args, msg):
    with pytest.raises(MetaError, match=msg):
        board_meta.plan_props(*args)


def test_remove_accepts_values_not_in_catalog():
    assert board_meta.plan_props(remove={"perifericos": "viejo"}) == {"perifericos": ("remove", ["viejo"])}


def test_set_meta_note_and_props():
    f = DevicesFile()
    f.update_device_key(MAC, "mi-placa")
    now = dt.datetime(2026, 10, 6, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=-3)))
    r = f.set_meta(MAC, "alejo", note="testeando", props_ops=board_meta.plan_props({"chip": "esp32"}), now=now)
    assert r["note_changed"] and r["props_changes"] == {"chip": {"from": None, "to": "esp32"}}
    e = f.get_all()[MAC]
    assert (e["device_key"], e["note"], e["note_by"], e["note_at"], e["props"]) == (
        "mi-placa", "testeando", "alejo", "2026-10-06T10:00:00-03:00", {"chip": "esp32"})
    assert f.props_in_use("chip", "esp32") == ["mi-placa"] and f.props_in_use("chip", "esp32-c3") == []
    r = f.set_meta(MAC, "otro", note="testeando")
    assert not r["note_changed"] and f.get_all()[MAC]["note_by"] == "alejo"
    f.set_meta(MAC, "alejo", note="", props_ops=board_meta.plan_props({"chip": None}))
    assert f.get_all()[MAC] == {"device_key": "mi-placa", "hw_model": None}
    with pytest.raises(KeyError):
        f.set_meta("11:22:33:44:55:66", "x", note="hola")


def _add(i):
    DevicesFile().set_meta(MAC, f"u{i}", props_ops={"uso": ("add", [f"v{i}"])})


def test_set_meta_from_several_processes_keeps_all():
    """Cada set_meta es un leer-modificar-escribir con el flock: los procesos no se pisan."""
    DevicesFile().update_device_key(MAC, "p")
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_add, args=(i,)) for i in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    assert sorted(DevicesFile().get_all()[MAC]["props"]["uso"]) == sorted(f"v{i}" for i in range(8))

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
    assert list(cats) == ["estado", "uso", "chip", "conectividad"]
    assert [c["multi"] for c in cats.values()] == [False, True, False, True]
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
    assert ops == {"chip": [("set", ["esp32-s3"])], "estado": [("set", [])], "conectividad": [("add", ["lte", "wifi"])],
                   "uso": [("remove", ["ci"])]}
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
    (("chip=esp32",), "categoría: valor"), (({"chip": "esp32"}, {"chip": "esp32-s3"}), "dos veces"),
])
def test_plan_props_rejects(args, msg):
    with pytest.raises(MetaError, match=msg):
        board_meta.plan_props(*args)


def test_remove_accepts_values_not_in_catalog():
    assert board_meta.plan_props(remove={"uso": "viejo"}) == {"uso": [("remove", ["viejo"])]}


def test_add_and_remove_in_the_same_category():
    """`set <dev> conectividad+=ble conectividad-=wifi`: las dos en un pedido."""
    ops = board_meta.plan_props(add={"conectividad": "ble"}, remove={"conectividad": "wifi"})
    new, changes = board_meta.apply_props({"conectividad": ["wifi", "lte"]}, ops)
    assert new == {"conectividad": ["lte", "ble"]}
    assert board_meta.apply_props({"uso": ["ci"]}, board_meta.plan_props({"uso": ["demo"]}, {"uso": "ci"}))[0] == \
        {"uso": ["demo", "ci"]}


def test_set_meta_note_and_props():
    f = DevicesFile()
    f.update_device_key(MAC, "mi-placa")
    now = dt.datetime(2026, 10, 6, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=-3)))
    r = f.set_meta(MAC, "alejo", note="testeando", props={"chip": "esp32"}, now=now)
    assert r["note_changed"] and r["props_changes"] == {"chip": {"from": None, "to": "esp32"}}
    e = f.get_all()[MAC]
    assert (e["device_key"], e["note"], e["note_by"], e["note_at"], e["props"]) == (
        "mi-placa", "testeando", "alejo", "2026-10-06T10:00:00-03:00", {"chip": "esp32"})
    assert f.props_in_use("chip", "esp32") == ["mi-placa"] and f.props_in_use("chip", "esp32-c3") == []
    r = f.set_meta(MAC, "otro", note="testeando")
    assert not r["note_changed"] and f.get_all()[MAC]["note_by"] == "alejo"
    f.set_meta(MAC, "alejo", note="", props={"chip": None})
    assert f.get_all()[MAC] == {"device_key": "mi-placa", "hw_model": None}
    with pytest.raises(KeyError):
        f.set_meta("11:22:33:44:55:66", "x", note="hola")


def _add(i):
    board_meta.add_value("uso", f"v{i}")
    DevicesFile().set_meta(MAC, f"u{i}", props_add={"uso": f"v{i}"})


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


@pytest.mark.parametrize("bad", ["a‮b", "a​b", "x⁦y", "﻿hola"])
def test_clean_note_rejects_format_characters(bad):
    with pytest.raises(MetaError, match="formato"):
        board_meta.clean_note(bad)


def test_values_live_in_a_dir_writable_by_the_api_even_if_esp_base_is_read_only():
    """En la Pi /opt/esp es root 755 y el api corre como sfypi: el catálogo va en meta/
    (install.sh lo crea 777). Antes iba en /opt/esp → PermissionError al agregar un valor."""
    base = paths.esp_base()
    paths.meta_dir().mkdir(parents=True)
    base.chmod(0o555)
    try:
        board_meta.add_value("chip", "esp32-p4")
        board_meta.remove_value("chip", "esp32-p4", lambda c, v: [])
    finally:
        base.chmod(0o755)
    assert paths.properties_file().parent == paths.meta_dir()
    assert not (base / "properties.json").exists()


def test_legacy_properties_file_is_read_and_migrated_on_write():
    paths.esp_base().mkdir(parents=True, exist_ok=True)
    paths.legacy_properties_file().write_text(json.dumps({"values": {"chip": [{"id": "esp32-p4"}]}}))
    assert [v["id"] for v in cat_by_id()["chip"]["values"]] == ["esp32-p4"]
    board_meta.add_value("chip", "esp32-h2")
    data = json.loads(paths.properties_file().read_text())
    assert [v["id"] for v in data["values"]["chip"]] == ["esp32-p4", "esp32-h2"]


def test_corrupt_catalog_is_kept_as_bad_copy_before_being_overwritten():
    paths.meta_dir().mkdir(parents=True)
    paths.properties_file().write_text('{"values": {"chip": [roto')
    assert len(cat_by_id()["chip"]["values"]) == 3                   # set inicial
    board_meta.add_value("chip", "esp32-p4")
    assert pathlib.Path(str(paths.properties_file()) + ".bad").read_text() == '{"values": {"chip": [roto'


def test_set_meta_validates_against_the_catalog_with_the_lock():
    """La validación va dentro del leer-modificar-escribir: un valor borrado entre que el api
    lo chequeó y escribió no se cuela."""
    f = DevicesFile()
    f.update_device_key(MAC, "p")
    board_meta.add_value("chip", "esp32-p4")
    board_meta.remove_value("chip", "esp32-p4", lambda c, v: [])
    with pytest.raises(MetaError, match="esp32-p4"):
        f.set_meta(MAC, "x", props={"chip": "esp32-p4"})


def test_locked_holds_the_flock_of_devices_json():
    """DevicesFile.locked: otro proceso que quiere escribir devices.json espera."""
    import threading
    import time
    f = DevicesFile()
    f.update_device_key(MAC, "p")
    order = []

    def hold(data):
        order.append("dentro")
        time.sleep(0.3)
        order.append("sale")
        return list(data)
    t = threading.Thread(target=lambda: f.locked(hold))
    t.start()
    time.sleep(0.1)
    ctx = multiprocessing.get_context("fork")
    p = ctx.Process(target=lambda: DevicesFile().update_device_key(MAC, "q"))
    p.start()
    p.join()
    t.join()
    assert order == ["dentro", "sale"] and DevicesFile().get_all()[MAC]["device_key"] == "q"

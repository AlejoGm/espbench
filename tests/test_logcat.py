"""logcat: categoría y nivel de cada línea del log, catálogo del bench, filtro y GET /api/log-categories."""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server import api, logcat, paths  # noqa: E402


def run(lines, cats=None):
    c = logcat.Classifier(cats if cats is not None else logcat.DEFAULT_CATEGORIES)
    return [c.feed(origin, body) for origin, body in lines]


def test_idf_line_category_by_tag_and_level_by_letter():
    assert run([
        (">", "I (12) BG95 ./lib/QuectelBG95/QuectelBG95.cpp:1955 GetMCCMNC(): MCC 722, MNC 7"),
        (">", "E (12) MB_CONTROLLER_MASTER: mbc_master_get_parameter(74): Master get parameter failure"),
        (">", "I (1010) Controller Config Danfoss: Danfoss DANFOSS-EETa-3W - FW Version: 151"),
        (">", "W (5) esp-tls: couldn't get hostname"),
        (">", "I (32) boot.esp32: SPI Speed      : 40MHz"),
        (">", "D (5) TagNuevo: algo"),
        (">", "I (12:34:56.789) wifi:mode : sta"),                  # hora del sistema en vez de ms
    ]) == [("celular", "I"), ("controlador", "E"), ("controlador", "I"), ("nube", "W"), ("sistema", "I"),
           ("otros", "D"), ("wifi-ble", "I")]


def test_exact_tag_wins_over_prefix_and_first_category_wins():
    cats = [{"id": "a", "label": "A", "tags": ["Controller*"], "lines": ["^dup"]},
            {"id": "b", "label": "B", "tags": ["ControllerX", "Controller*"], "lines": ["^dup", "^solo-b"]}]
    assert run([(">", "I (1) ControllerX: x"), (">", "I (1) ControllerY: y"),
                (">", "dup"), (">", "solo-b")], cats) == [("b", "I"), ("a", "I"), ("a", None), ("b", None)]


def test_untagged_lines_regex_panic_reset_and_inheritance():
    assert run([
        (">", "hola"),                                              # sin anterior: otros
        (">", "I (1) BG95 ./x.cpp:1 f(): estado"),
        (">", "Unregistered"),                                      # regex de celular, sin nivel
        (">", "texto suelto"),                                      # hereda la anterior
        (">", "Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled."),
        (">", "Backtrace: 0x400d1234:0x3ffb0000"),                  # hereda el panic: sistema, E
        (">", "Rebooting..."),
        (">", "rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)"),
        (">", "load:0x3fff0030,len:7104"),                          # ROM: sistema, sin nivel
        ("↪", "sigue"),
        (">", "HBMON t=67 int_free=36531 int_big=22528"),
        (">", "  applying 3/40"),                                     # SfyParameters aplicando: controlador
        (">", "E (9) task_wdt: Task watchdog got triggered."),
    ]) == [("otros", None), ("celular", "I"), ("celular", None), ("celular", None), ("sistema", "E"),
           ("sistema", "E"), ("sistema", "E"), ("sistema", "W"), ("sistema", None), ("sistema", None),
           ("memoria", None), ("controlador", None), ("sistema", "E")]


def test_server_lines_are_banco_with_taglog_level():
    assert run([("|", "WARN  | protocol       | algo"), ("|", "INFO  | devicelog      | sesión"),
                (None, "2026-10-05 16:00:00 | ERROR | flash          | falló"), ("|", "raro")]) == [
        ("banco", "W"), ("banco", "I"), ("banco", "E"), ("banco", None)]
    # una línea serial después de una del server no hereda "banco"
    assert run([("|", "WARN  | flash          | x"), (">", "suelta")]) == [("banco", "W"), ("banco", "W")]


def test_parse_filter_and_passes():
    cat = logcat.load()
    assert logcat.parse_filter("celular, nube", "e,W", cat) == ({"celular", "nube"}, {"E", "W"})
    assert logcat.parse_filter(None, "", cat) == (None, None)
    assert logcat.parse_filter("banco,otros", None, cat) == ({"banco", "otros"}, None)
    with pytest.raises(ValueError, match="radio no existe.*celular"):
        logcat.parse_filter("radio", None, cat)
    with pytest.raises(ValueError, match="nivel X"):
        logcat.parse_filter(None, "X", cat)
    assert logcat.passes(("celular", "D"), None, {"I"})            # I incluye D y V
    assert not logcat.passes(("celular", None), None, {"I"})       # sin nivel no pasa un filtro de nivel
    assert not logcat.passes(("nube", "E"), {"celular"}, None)
    assert logcat.passes(("nube", "E"), {"nube"}, {"E", "W"})


def test_bench_catalog_replaces_default_and_broken_falls_back():
    f = paths.log_categories_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"categories": [{"id": "radio", "label": "Radio", "tags": ["LORA*"], "lines": ["^RX "]}]}))
    c = logcat.load()
    assert c["source"] == "bench" and c["error"] is None and [x["id"] for x in c["categories"]] == ["radio"]
    assert logcat.ids(c) == ["radio", "banco", "otros"]
    assert run([(">", "I (1) LORA_MAC: x"), (">", "RX 12"), (">", "I (1) BG95: y")], c["categories"]) == [
        ("radio", "I"), ("radio", None), ("otros", "I")]
    f.write_text("{roto")
    c = logcat.load()
    assert c["source"] == "default" and c["categories"] is logcat.DEFAULT_CATEGORIES
    assert c["error"].startswith("log_categories.json:")
    f.unlink()
    assert logcat.load()["error"] is None


@pytest.mark.parametrize("bad", [
    {"categories": []},
    {"nada": 1},
    {"categories": [{"id": "Mayus"}]},
    {"categories": [{"id": "banco"}]},                          # reservada
    {"categories": [{"id": "a"}, {"id": "a"}]},
    {"categories": [{"id": "a", "lines": ["("]}]},
    {"categories": [{"id": "a", "tags": ["*"]}]},
    {"categories": [{"id": "a", "label": "x" * 25}]},
])
def test_validate_rejects(bad):
    with pytest.raises(logcat.CatalogError):
        logcat.validate(bad)


def test_default_catalog_is_valid():
    assert logcat.validate({"categories": logcat.DEFAULT_CATEGORIES}) == logcat.DEFAULT_CATEGORIES


def test_api_log_categories():
    r = api.log_categories()
    ids = [c["id"] for c in r["categories"]]
    assert ids[:2] == ["celular", "controlador"] and ids[-2:] == ["banco", "otros"]
    assert r["categories"][-1]["builtin"] and "builtin" not in r["categories"][0]
    assert r["levels"] == ["E", "W", "I"] and r["source"] == "default" and r["error"] is None

"""Paridad entre la categoría de línea del dashboard (EB.logCategorizer, espbench.js) y
server/logcat.py: la misma secuencia de líneas tiene que dar las mismas (categoría, nivel),
con la herencia incluida. El JS recibe el catálogo de GET /api/log-categories. Se saltea sin node."""
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "remote"))
from server import logcat  # noqa: E402
from server.logrange import Line  # noqa: E402

D = "2026-11-25 10:00:00.000"
CASES = [
    f"{D} | INFO  | devicelog      | sesión 20261125_100000_1 tty=ttyUSB0",
    f"{D} > hola sin tag",
    f"{D} > rst:0x1 (POWERON_RESET),boot:0x17 (SPI_FAST_FLASH_BOOT)",
    f"{D} > configsip: 0, SPIWP:0xee",
    f"{D} > ets Jul 29 2019 12:21:46",
    f"{D} > I (32) boot: ESP-IDF v5.3.2 2nd stage bootloader",
    f"{D} > \x1b[0;32mI (1302) app_init: Project name:     SFY1-57_0\x1b[0m",
    f"{D} > [BOOTSEQ]    780 main_ControllerDriverInit()",
    f"{D} > I (780) MAIN ./main/_src/main.cpp:351 main_ControllerDriverInit(): init",
    f"{D} > I (800) PORT_MODBUS: Modbus master initialized",
    f"{D} > I (1010) Controller Config Danfoss: Danfoss DANFOSS-EETa-3W - FW Version: 151",
    f"{D} > \x1b[0;31mE (20) MB_CONTROLLER_MASTER: mbc_master_get_parameter(74): failure\x1b[0m",
    f"{D} > bits_00:\t10000001\t\t(0x81)",
    f"{D} > tint:\t\t-990\t\t\t(0xFC22)",
    f"{D} > I (5) BG95 ./lib/QuectelBG95/QuectelBG95.cpp:4376 getRegistrationStatus(): [CREG: 0]",
    f"{D} > Unregistered",
    f"{D} ↪ Searching...",
    f"{D} > E (6) EG912 ./lib/x.cpp:1 CheckSimStatus(): (U)SIM not inserted",
    f"{D} > W (7) MQTT ./m.cpp:3 Connect(): reintento",
    f"{D} > MQTT enviando... 04 00 81 22",
    f"{D} > W (8) WDrv ./w.cpp:1 esp_on_wifi_disconnect(): STA disconnected: reason 201",
    f"{D} > I (9) bleGAP: adv",
    f"{D} > I (10) OTA ./sfy-core/x.cpp:505 doOTA_gsm(): Download Part 1 OK",
    f"{D} > Installing...  5120/1875200  [0.27 %] (58310 ms)",
    f"{D} > .",
    f"{D} > HBMON t=67 int_free=36531 int_big=22528 int_min=26847",
    f"{D} > I (11) freeheap: 123",
    f"{D} > D (12) TagNuevo: algo",
    f"{D} > V (12) Otro: algo",
    f"{D} > suelta después de un tag desconocido",
    f"{D} > ░░░░░░▓██████████▒░░░░░▒███████████░░░",
    f"{D} > Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled.",
    f"{D} > Backtrace: 0x400d1234:0x3ffb0000 0x400d5678:0x3ffb0010",
    f"{D} > abort() was called at PC 0x400d1234 on core 0",
    f"{D} > Rebooting...\r",
    f"{D} > algo\rI (13) SCAN ./s.cpp:1 try_connect_aps(): APs not found",
    f"{D} > E (14) task_wdt: Task watchdog got triggered.",
    f"{D} | WARN  | protocol       | algo del server",
    f"{D} | raro",
    f"{D} > ",
    "2026-10-05 16:00:00 | ERROR | flash          | falló (log viejo sin prefijo)",
    "I (1) BG95: línea sin prefijo de un log viejo",
    "sin prefijo y sin tag",
]


def py_classify(catalog_cats, lines):
    c = logcat.Classifier(catalog_cats)
    out = []
    for text in lines:
        line = Line(0, text.encode("utf-8"))
        out.append(list(c.feed(line.origin, line.clean())))
    return out


def js_classify(catalog, lines):
    script = ("const EB = require(process.argv[1]);"
              "const inp = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              "const c = EB.logCategorizer(inp.catalog); let prev = null;"
              "process.stdout.write(JSON.stringify(inp.lines.map(l => { prev = c.classify(l, prev);"
              " return [prev.cat, prev.lvl]; })));")
    r = subprocess.run(["node", "-e", script, str(ROOT / "remote/dashboard/espbench.js")],
                       input=json.dumps({"catalog": catalog, "lines": lines}),
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node no instalado")
def test_logcategorizer_matches_logcat():
    catalog = logcat.public({"categories": logcat.DEFAULT_CATEGORIES, "source": "default", "error": None})
    py = py_classify(logcat.DEFAULT_CATEGORIES, CASES)
    js = js_classify(catalog, CASES)
    assert js == py, [(c, j, p) for c, j, p in zip(CASES, js, py) if j != p]
    # cubre todas las categorías y todos los niveles
    assert {c for c, _ in py} == set(logcat.ids({"categories": logcat.DEFAULT_CATEGORIES}))
    assert {lv for _, lv in py} == {"E", "W", "I", "D", "V", None}


@pytest.mark.skipif(shutil.which("node") is None, reason="node no instalado")
def test_custom_catalog_parity():
    cats = [{"id": "a", "label": "A", "tags": ["Controller*", "X"], "lines": ["^dup", "^\\s*solo"]},
            {"id": "b", "label": "B", "tags": ["ControllerX", "X"], "lines": ["^dup", "fin$"]}]
    lines = [f"{D} > I (1) ControllerX: x", f"{D} > I (1) ControllerY: y", f"{D} > W (1) X: z",
             f"{D} > dup", f"{D} >   solo", f"{D} > algo fin", f"{D} > suelta"]
    catalog = logcat.public({"categories": cats, "source": "bench", "error": None})
    assert js_classify(catalog, lines) == py_classify(cats, lines)

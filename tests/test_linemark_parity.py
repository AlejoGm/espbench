"""Paridad entre la marca de línea del dashboard (EB.lineMark, espbench.js) y
serial_watch.line_kind (Python): las mismas líneas tienen que dar el mismo
boot/panic/None. Las regex están copiadas en los dos lados; esto avisa si una
cambia y la otra no. Se saltea sin node."""
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "remote"))
from server.serial_watch import line_kind  # noqa: E402

CASES = [
    "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)",
    "rst:0xc (SW_CPU_RESET),boot:0x13",
    "rst:0x10 (RTCWDT_RTC_RESET)",
    "rst:0x1 (poweron)",                       # minúsculas: no
    "rst:0x1 POWERON_RESET",                   # sin paréntesis: no
    "ets Jun  8 2016 00:22:57 rst:0x8 (TG1WDT_SYS_RESET)",
    "Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled.",
    "Guru Meditation Error: Core 0 panic'ed",  # sin el motivo: no
    "abort() was called at PC 0x400d1234 on core 0",
    "Brownout detector was triggered",
    "E (123) task_wdt: Task watchdog got triggered. The following tasks did not reset the watchdog in time:",
    "***ERROR*** A stack overflow in task main has been detected.",
    "***ERROR*** A stack overflow in task ",   # sin nombre: no
    "assert failed: app_main main.c:12 (x == 1)",
    "Backtrace: 0x400d1234:0x3ffb0000",
    "Rebooting...",
    "I (120) main: listo",
    "",
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node no instalado")
def test_linemark_matches_line_kind():
    script = ("const EB = require(process.argv[1]);"
              "const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              "process.stdout.write(JSON.stringify(cases.map(c => EB.lineMark(c))));")
    r = subprocess.run(["node", "-e", script, str(ROOT / "remote/dashboard/espbench.js")],
                       input=json.dumps(CASES), capture_output=True, text=True, check=True)
    js = json.loads(r.stdout)
    py = [line_kind(c) for c in CASES]
    assert js == py, [(c, j, p) for c, j, p in zip(CASES, js, py) if j != p]
    # también con el prefijo del DeviceLog (serial) y en una continuación ↪
    r = subprocess.run(["node", "-e", script, str(ROOT / "remote/dashboard/espbench.js")],
                       input=json.dumps([f"2026-10-06 10:00:00.000 {o} {c}" for c in CASES for o in ">↪"]),
                       capture_output=True, text=True, check=True)
    assert json.loads(r.stdout) == [p for p in py for _ in ">↪"]

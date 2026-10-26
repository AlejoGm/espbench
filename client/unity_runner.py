# -*- coding: utf-8 -*-
"""
`espbench test`: corre un test app de Unity (ESP-IDF) en una placa y devuelve el resultado por test.

El test app abre el menú de Unity por la consola (`unity_run_menu()`): Enter lista los tests, un número corre uno, y
un test multietapa (`TEST_CASE_MULTIPLE_STAGES`) pide además la etapa. Lo de acá:

- Corre los tests **de a uno**, con un tope por test: uno que se cuelga falla solo y la corrida sigue. Correrlos por
  tag en el menú no sirve: un multietapa adentro de la tanda la frena pidiendo la etapa.
- Un multietapa se sigue a través de los reinicios: manda la etapa, espera el reinicio (`Press ENTER`) o el resumen, y
  manda la siguiente. Los reinicios son esperables, así que un `boot_loop` en el medio no corta.
- Un panic se le atribuye al test que lo tuvo, con su pedazo de log, y la corrida sigue después del reinicio.

Sin `input()` ni `print`: lo llama el CLI (`client/espbench.py`), que decide la salida.
"""
import dataclasses
import json
import pathlib
import re
import time
from typing import Callable, List, Optional
from xml.etree import ElementTree as ET

MENU = "Enter test for running"                         # el menú terminó de listar
PRESS_ENTER = "Press ENTER to see the list of tests"    # arranque del test app (o reinicio de una etapa)
SUMMARY = r"\d+ Tests \d+ Failures \d+ Ignored"

_CASE_RE = re.compile(r'^\((\d+)\)\s+"(.*)"\s*(.*)$')
_RESULT_RE = re.compile(r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):(?P<name>.+?):(?P<res>PASS|FAIL|IGNORE)"
                        r"(?::\s?(?P<msg>.*))?$")
_SUMMARY_RE = re.compile(r"^(\d+) Tests (\d+) Failures (\d+) Ignored")

PASS, FAIL, IGNORED, TIMEOUT, PANIC, SKIPPED = "pass", "fail", "ignored", "timeout", "panic", "skipped"
FAILED = (FAIL, TIMEOUT, PANIC)


def text_of(line: str) -> str:
    """El texto de una línea del log (`HH:MM:SS.mmm > texto`), sin la hora ni el origen."""
    for sep in (" > ", " ↪ ", " | "):
        i = line.find(sep)
        if 0 <= i <= 16:
            return line[i + len(sep):]
    return line


@dataclasses.dataclass
class Case:
    number: int
    name: str
    tags: List[str]
    stages: List[str] = dataclasses.field(default_factory=list)

    @property
    def multi_stage(self) -> bool:
        return bool(self.stages)

    @property
    def multi_device(self) -> bool:
        return "multi_device" in self.tags


def parse_menu(lines: List[str]) -> List[Case]:
    """Los tests del menú: `(N)\\t"nombre" [tag][tag]`, y debajo, con un tab, las etapas de un multietapa."""
    cases: List[Case] = []
    for raw in lines:
        t = text_of(raw).rstrip("\r\n")
        m = _CASE_RE.match(t.strip())
        if not m:
            continue
        if t.startswith(("\t", " ")) and cases:
            cases[-1].stages.append(m.group(2))
        else:
            cases.append(Case(int(m.group(1)), m.group(2), re.findall(r"\[([^\]]+)\]", m.group(3))))
    return cases


def parse_tags(spec: Optional[str]) -> List[str]:
    """`[ota][modem]` o `ota,modem`."""
    if not spec:
        return []
    found = re.findall(r"\[([^\]]+)\]", spec)
    return found or [t.strip() for t in spec.split(",") if t.strip()]


def select(cases: List[Case], tags: List[str]) -> List[Case]:
    """Los que tienen todas las tags pedidas."""
    return [c for c in cases if all(t in c.tags for t in tags)]


def parse_result(lines: List[str], name: str) -> Optional[dict]:
    """La línea de resultado de Unity de ese test: `archivo:línea:nombre:PASS|FAIL[: mensaje]|IGNORE`."""
    for raw in lines:
        m = _RESULT_RE.match(text_of(raw).strip())
        if m and m.group("name") == name:
            res = {"PASS": PASS, "FAIL": FAIL, "IGNORE": IGNORED}[m.group("res")]
            return {"result": res, "file": m.group("file"), "line": int(m.group("line")),
                    "message": (m.group("msg") or "").strip()}
    return None


def has_summary(lines: List[str]) -> bool:
    return any(_SUMMARY_RE.match(text_of(l).strip()) for l in lines)


def project_info(build_dir: Optional[pathlib.Path]) -> dict:
    """Nombre y versión del binario, de `project_description.json` del build dir (si está)."""
    if build_dir is None:
        return {}
    try:
        d = json.loads((build_dir / "project_description.json").read_text())
    except (OSError, ValueError):
        return {}
    return {k: d.get(k) for k in ("project_name", "project_version", "idf_ver", "target") if d.get(k)}


class Runner:
    """Maneja el menú de Unity de una placa a través de la API de espbench (`client.send` + `client.read_range`)."""

    def __init__(self, client, board, case_timeout_s: float, menu_timeout_s: float = 30.0,
                 boot_timeout_s: float = 90.0, log_tail: int = 40, on_progress: Optional[Callable] = None):
        self.c, self.board = client, board
        self.case_timeout_s, self.menu_timeout_s, self.boot_timeout_s = case_timeout_s, menu_timeout_s, boot_timeout_s
        self.log_tail = log_tail
        self.progress = on_progress or (lambda msg: None)

    # ----- consola -----

    def _send(self, text: str, until: str, timeout_s: float, fail_on=("panic", "boot_loop")) -> dict:
        s = self.c.send(self.board, text, enter=True)
        cursor = s.get("cursor")
        if not cursor:
            return {"ok": False, "error": "unexpected", "lines": []}
        return self.c.read_range(self.board.key, since=cursor, until=until, echo=text, timeout_s=timeout_s,
                                 fail_on=fail_on)

    def _wait(self, since: Optional[str], until: str, timeout_s: float) -> dict:
        """Desde `since` y no desde ahora: el reinicio pudo haber terminado antes de que se pregunte."""
        return self.c.read_range(self.board.key, since=since or "now", until=until, timeout_s=timeout_s, fail_on=())

    def menu(self) -> List[Case]:
        """Pide el menú y lo lee."""
        r = self._send("", until=MENU, timeout_s=self.menu_timeout_s)
        if not r.get("until_found"):
            return []
        return parse_menu(r.get("lines") or [])

    def _recover(self) -> None:
        """Después de un test colgado: reset y esperar el menú de nuevo."""
        s = self.c.command(self.board, "reset")
        self._wait(s.get("cursor"), PRESS_ENTER, self.boot_timeout_s)

    # ----- un test -----

    def run_case(self, case: Case) -> dict:
        out = {"number": case.number, "name": case.name, "tags": case.tags}
        if case.multi_device:
            return {**out, "result": SKIPPED, "message": "multi_device: necesita más de una placa", "duration_s": 0}
        t0 = time.monotonic()
        res = self._run_multi(case) if case.multi_stage else self._run_single(case)
        res["duration_s"] = round(time.monotonic() - t0, 3)
        if res["result"] not in FAILED:
            res.pop("log", None)
        return {**out, **res}

    def _tail(self, lines: List[str]) -> List[str]:
        return lines[-self.log_tail:]

    def _run_single(self, case: Case) -> dict:
        if not self.menu():
            return {"result": FAIL, "message": "no apareció el menú de Unity"}
        r = self._send(str(case.number), until=f"re:{SUMMARY}", timeout_s=self.case_timeout_s)
        return self._outcome(case, r)

    def _run_multi(self, case: Case) -> dict:
        last = len(case.stages)
        for stage in range(1, last + 1):
            if not self.menu():
                return {"result": FAIL, "message": f"no apareció el menú antes de la etapa {stage}"}
            sub = self._send(str(case.number), until=rf're:^\s*\({last}\)\s+"', timeout_s=self.menu_timeout_s)
            if not sub.get("until_found"):
                return {"result": FAIL, "message": f"no aparecieron las etapas (etapa {stage})",
                        "log": self._tail(sub.get("lines") or [])}
            self.progress(f"  etapa {stage}/{last}: {case.stages[stage - 1]}")
            # Una etapa que reinicia es lo esperado: un boot_loop no corta, un panic sí.
            r = self._send(str(stage), until=f"re:{SUMMARY}|{PRESS_ENTER}", timeout_s=self.case_timeout_s,
                           fail_on=("panic",))
            lines = r.get("lines") or []
            if r.get("error") == "crashed" or not r.get("until_found") or has_summary(lines):
                res = self._outcome(case, r)
                res["stage"] = stage
                return res
        return {"result": FAIL, "message": "la última etapa reinició sin dar resultado"}

    def _outcome(self, case: Case, r: dict) -> dict:
        lines = r.get("lines") or []
        if r.get("error") == "crashed":
            crash = r.get("crash") or {}
            self._wait(crash.get("cursor") or r.get("start"), PRESS_ENTER, self.boot_timeout_s)  # el panic reinicia
            return {"result": PANIC, "message": (crash.get("detail") or {}).get("reason") or "panic",
                    "crash": crash, "log": self._tail(lines)}
        if not r.get("until_found"):
            self._recover()
            return {"result": TIMEOUT, "message": f"sin resultado en {self.case_timeout_s:.0f} s",
                    "log": self._tail(lines)}
        parsed = parse_result(lines, case.name)
        if parsed is None:
            return {"result": FAIL, "message": "terminó sin la línea de resultado de Unity", "log": self._tail(lines)}
        return {**parsed, "log": self._tail(lines)}

    # ----- la corrida -----

    def run(self, cases: List[Case]) -> List[dict]:
        results = []
        for i, case in enumerate(cases, 1):
            self.progress(f"[{i}/{len(cases)}] ({case.number}) {case.name}")
            res = self.run_case(case)
            self.progress(f"  -> {res['result']} ({res['duration_s']:.1f} s)")
            results.append(res)
        return results


def summarize(results: List[dict]) -> dict:
    count = {k: sum(1 for r in results if r["result"] == k) for k in (PASS, FAIL, TIMEOUT, PANIC, IGNORED, SKIPPED)}
    return {"total": len(results), "failed": sum(count[k] for k in FAILED), **count}


def junit(results: List[dict], path: pathlib.Path, suite: str, properties: dict) -> None:
    """Un `<testsuite>` con un `<testcase>` por test. Un timeout o un panic va como `<error>`, un FAIL como `<failure>`."""
    s = summarize(results)
    root = ET.Element("testsuites", name="espbench test")
    ts = ET.SubElement(root, "testsuite", name=suite, tests=str(s["total"]), failures=str(s[FAIL]),
                       errors=str(s[TIMEOUT] + s[PANIC]), skipped=str(s[IGNORED] + s[SKIPPED]),
                       time=f"{sum(r.get('duration_s') or 0 for r in results):.3f}")
    props = ET.SubElement(ts, "properties")
    for k, v in properties.items():
        if v is not None:
            ET.SubElement(props, "property", name=str(k), value=str(v))
    for r in results:
        tc = ET.SubElement(ts, "testcase", classname=suite, name=r["name"], time=f"{r.get('duration_s') or 0:.3f}")
        if r.get("file"):
            tc.set("file", r["file"])
            tc.set("line", str(r.get("line")))
        log = "\n".join(r.get("log") or [])
        if r["result"] == FAIL:
            ET.SubElement(tc, "failure", message=r.get("message") or "FAIL").text = log
        elif r["result"] in (TIMEOUT, PANIC):
            ET.SubElement(tc, "error", message=f"{r['result']}: {r.get('message') or ''}").text = log
        elif r["result"] in (IGNORED, SKIPPED):
            ET.SubElement(tc, "skipped", message=r.get("message") or r["result"])
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)

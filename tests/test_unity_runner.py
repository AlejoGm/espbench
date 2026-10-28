"""`espbench test` (client/unity_runner.py): el parseo del menú de Unity de ESP-IDF, y la corrida entera contra la
Pi simulada con una placa que se comporta como un test app (`UnityApp`, abajo): listar, correr un test, multietapa
con reinicios, un test que falla, uno que se cuelga y uno con panic."""
import json
import pathlib
import sys
from xml.etree import ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "remote"))

from client import espbench_lib as lib  # noqa: E402
from client import unity_runner as unity  # noqa: E402
from tests.benchsim import boot_text  # noqa: E402
from tests.test_espbench_cli import bench, board, cli, write_project  # noqa: E402,F401  (fixtures)

MENU_LINES = [
    "14:19:50.187 > Here's the test menu, pick your combo:",
    '14:19:50.187 > (1)\t"arranca vacia" [eventstorage][flash]',
    '14:19:50.209 > (2)\t"un CRC roto: marca consumido" [eventstorage][flash]',
    '14:19:50.290 > (3)\t"corte de luz" [eventstorage][flash][power][multi_stage]',
    '14:19:50.290 > \t(1)\t"power_cut_before"',
    '14:19:50.290 > \t(2)\t"power_cut_after"',
    '14:19:50.291 > (4)\t"dos placas" [modem][multi_device]',
    "14:19:50.291 > ",
    "14:19:50.291 > Enter test for running.",
]


# ---------- parseo ----------

def test_menu_lists_cases_tags_and_stages():
    cases = unity.parse_menu(MENU_LINES)
    assert [(c.number, c.name) for c in cases] == [(1, "arranca vacia"), (2, "un CRC roto: marca consumido"),
                                                   (3, "corte de luz"), (4, "dos placas")]
    assert cases[0].tags == ["eventstorage", "flash"] and not cases[0].multi_stage
    assert cases[2].stages == ["power_cut_before", "power_cut_after"] and cases[2].multi_stage
    assert cases[3].multi_device


def test_tags_select_the_cases_that_have_all_of_them():
    cases = unity.parse_menu(MENU_LINES)
    assert unity.parse_tags("[eventstorage][power]") == unity.parse_tags("eventstorage, power") == \
        ["eventstorage", "power"]
    assert [c.number for c in unity.select(cases, ["eventstorage", "power"])] == [3]
    assert [c.number for c in unity.select(cases, [])] == [1, 2, 3, 4]


def test_result_line_with_a_colon_in_the_name_and_the_message():
    lines = ["15:00:00.000 > /x/test_es.cpp:226:un CRC roto: marca consumido:FAIL: Expected 1 Was 0. Function x",
             "15:00:00.001 > -----------------------", "15:00:00.001 > 1 Tests 1 Failures 0 Ignored "]
    r = unity.parse_result(lines, "un CRC roto: marca consumido")
    assert r == {"result": "fail", "file": "/x/test_es.cpp", "line": 226, "message": "Expected 1 Was 0. Function x"}
    assert unity.has_summary(lines)
    assert unity.parse_result(lines, "otro") is None


# ---------- una placa que es un test app ----------

class UnityApp:
    """El menú de `unity_run_menu()` de ESP-IDF sobre una SimBoard.

    `cases`: lista de (nombre, tags, comportamiento). El comportamiento es "pass", ("fail", mensaje), "hang",
    "panic", o una lista de comportamientos por etapa para un multietapa, donde "reboot" es una etapa que reinicia.
    """

    def __init__(self, board, cases):
        self.b, self.cases, self.awaiting = board, cases, None
        self.runs = []                                   # (número, etapa) en el orden en que se corrieron
        board._reply = self._reply
        board.boot = self.boot
        board.after_flash = self.boot

    def boot(self, reason="SW_CPU_RESET", code="0xc"):
        self.awaiting = None
        self.b.serial(boot_text(reason, code))
        self.b.serial(f"{unity.PRESS_ENTER}.\r\n")

    def _reply(self, cmd):
        if cmd == "":
            self.b.serial("\r\nHere's the test menu, pick your combo:\r\n")
            for n, (name, tags, behavior) in enumerate(self.cases, 1):
                self.b.serial(f'({n})\t"{name}" {tags}\r\n')
                if isinstance(behavior, list):
                    for k in range(1, len(behavior) + 1):
                        self.b.serial(f'\t({k})\t"stage_{k}"\r\n')
            self.b.serial("\r\nEnter test for running.\r\n")
            return
        if not cmd.isdigit():
            return
        if self.awaiting is not None:
            n, self.awaiting = self.awaiting, None
            name, _, stages = self.cases[n - 1]
            self.runs.append((n, int(cmd)))
            self._do(name, stages[int(cmd) - 1], last=int(cmd) == len(stages))
            return
        n = int(cmd)
        name, _, behavior = self.cases[n - 1]
        if isinstance(behavior, list):
            self.b.serial(f"{name}\r\n")
            for k in range(1, len(behavior) + 1):
                self.b.serial(f'\t({k})\t"stage_{k}"\r\n')
            self.awaiting = n
            return
        self.runs.append((n, None))
        self.b.serial(f"Running {name}...\r\n")
        self._do(name, behavior, last=True)

    def _do(self, name, behavior, last):
        if behavior == "hang":
            return
        if behavior == "panic":
            self.b.panic()
            return
        if behavior == "reboot" and not last:
            self.b.later(0.05, self.boot)
            return
        failed = isinstance(behavior, tuple)
        result = f"FAIL: {behavior[1]}" if failed else "PASS"
        self.b.serial(f"/src/test_x.cpp:42:{name}:{result}\r\n")
        self.b.serial(f"-----------------------\r\n1 Tests {int(failed)} Failures 0 Ignored \r\n")
        self.b.serial(("FAIL" if failed else "OK") + "\r\nEnter next test, or 'enter' to see menu\r\n")


def _app(board, cases):
    app = UnityApp(board, cases)
    app.boot()
    return app


CASES = [
    ("pasa", "[es][flash]", "pass"),
    ("falla", "[es][flash]", ("fail", "Expected 1 Was 0")),
    ("se cuelga", "[es]", "hang"),
    ("panic", "[es]", "panic"),
    ("corte de luz", "[es][power]", ["reboot", "pass"]),
]


# ---------- la corrida, por el CLI ----------

def test_each_case_runs_alone_and_the_run_reports_each_one(cli, board, tmp_path):
    app = _app(board, CASES)
    junit = tmp_path / "r.xml"
    code, r = cli("test", "sim-board", "--no-flash", "--timeout-test", "2s", "--junit", str(junit))
    assert (code, r["error"]) == (11, "tests_failed")
    got = {t["name"]: t["result"] for t in r["tests"]}
    assert got == {"pasa": "pass", "falla": "fail", "se cuelga": "timeout", "panic": "panic", "corte de luz": "pass"}
    assert r["summary"] == {"total": 5, "failed": 3, "pass": 2, "fail": 1, "timeout": 1, "panic": 1, "ignored": 0,
                            "skipped": 0}
    falla = next(t for t in r["tests"] if t["name"] == "falla")
    assert falla["message"] == "Expected 1 Was 0" and falla["line"] == 42 and falla["log"]
    assert "log" not in next(t for t in r["tests"] if t["name"] == "pasa")      # solo las fallas traen log
    # El cuelgue y el panic no cortaron la corrida: el multietapa del final corrió sus dos etapas.
    assert app.runs[-2:] == [(5, 1), (5, 2)]

    suite = ET.parse(junit).getroot().find("testsuite")
    assert (suite.get("tests"), suite.get("failures"), suite.get("errors")) == ("5", "1", "2")


def test_a_multi_stage_case_that_fails_in_the_first_stage(cli, board):
    _app(board, [("corte", "[es]", [("fail", "antes del corte"), "pass"])])
    code, r = cli("test", "sim-board", "--no-flash", "--timeout-test", "2s")
    assert code == 11
    (t,) = r["tests"]
    assert (t["result"], t["stage"], t["message"]) == ("fail", 1, "antes del corte")


def test_tags_and_list(cli, board):
    app = _app(board, CASES)
    code, r = cli("test", "sim-board", "--no-flash", "--tags", "[es][power]", "--list")
    assert code == 0 and [c["name"] for c in r["cases"]] == ["corte de luz"]
    assert r["cases"][0]["stages"] == ["stage_1", "stage_2"]
    assert app.runs == []                                    # --list no corre nada

    code, r = cli("test", "sim-board", "--no-flash", "--tags", "nada")
    assert (code, r["error"]) == (7, "not_found")


def test_flash_then_run_all_passing(cli, board, tmp_path):
    write_project(tmp_path)
    _app(board, [("pasa", "[es]", "pass"), ("corte", "[es]", ["reboot", "reboot", "pass"])])
    code, r = cli("test", "sim-board", "--timeout-test", "2s")
    assert code == 0 and r["ok"] and r["summary"]["pass"] == 2, r
    assert r["project_name"] == "SIM-1_0" and board.flashes == 1


def test_a_test_app_without_tests_says_why(cli, board):
    _app(board, [])
    code, r = cli("test", "sim-board", "--no-flash")
    assert r["error"] == "unexpected" and "WHOLE_ARCHIVE" in r["message"]


def test_multi_device_cases_are_skipped(cli, board):
    _app(board, [("dos placas", "[multi_device]", "pass"), ("pasa", "[es]", "pass")])
    code, r = cli("test", "sim-board", "--no-flash")
    assert code == 0 and [t["result"] for t in r["tests"]] == ["skipped", "pass"]


# ---------- lo que ve el dashboard ----------

def test_the_run_is_reported_to_the_dashboard(cli, board):
    """Cada paso llega a la Pi: la corrida queda en meta/test_runs/ (card, pestaña Tests) y como eventos `test` (marcas
    en el log); el panic del test no deja la placa en Panic."""
    from server import events, paths, testrun
    from server.device_registry import acked
    _app(board, CASES)
    code, r = cli("test", "sim-board", "--no-flash", "--timeout-test", "2s")
    assert code == 11
    run = testrun.detail(board.mac, __import__("time").time())
    assert (run["status"], run["total"], run["done"], run["passed"], run["failed"]) == ("done", 5, 5, 2, 3)
    assert [c["result"] for c in run["cases"]] == ["pass", "fail", "timeout", "panic", "pass"]
    assert all(c["cursor"] and c["end_cursor"] for c in run["cases"])
    phases = [e["detail"]["phase"] for e in events.read(paths.device_events_file(board.mac)) if e["type"] == "test"]
    assert phases == ["start"] + ["case", "result"] * 5 + ["end"]
    wins = testrun.windows(board.mac, __import__("time").time())
    panics = [e for e in events.read(paths.device_events_file(board.mac)) if e["type"] == "panic"]
    assert panics and all(testrun.in_windows(events_epoch(e), wins) for e in panics)
    assert acked({"last_panic": {"ts": panics[-1]["ts"]}}, None, wins)["panic"] is True


def events_epoch(ev):
    import datetime as dt
    return dt.datetime.fromisoformat(ev["ts"]).timestamp()


def test_a_bench_that_does_not_know_about_tests_does_not_stop_the_run(cli, board, monkeypatch):
    from client import espbench_lib as lib

    def old_bench(self, board, **body):
        raise lib.EspbenchError("not_found", "HTTP 404")
    monkeypatch.setattr(lib.Client, "test_event", old_bench)
    _app(board, [("pasa", "[es]", "pass"), ("otro", "[es]", "pass")])
    code, r = cli("test", "sim-board", "--no-flash")
    assert code == 0 and [t["result"] for t in r["tests"]] == ["pass", "pass"]


# ---------- la red con la Pi ----------

def _flaky_reads(monkeypatch, fail):
    """Hace que `read_range` dé `network` según `fail(n)` (n = número de lectura, desde 1). Sin pausa entre intentos."""
    monkeypatch.setattr(unity, "NET_RETRY_PAUSE_S", 0)
    real, n = lib.Client.read_range, [0]

    def read_range(self, *a, **k):
        n[0] += 1
        if fail(n[0]):
            raise lib.EspbenchError("network", "sin conexión con la Pi: timed out")
        return real(self, *a, **k)
    monkeypatch.setattr(lib.Client, "read_range", read_range)


def test_a_read_that_times_out_once_is_retried(cli, board, monkeypatch):
    _app(board, [("pasa", "[es]", "pass"), ("corte", "[es]", ["reboot", "pass"]), ("otro", "[es]", "pass")])
    _flaky_reads(monkeypatch, lambda n: n in (4, 9))       # una lectura de un test simple y una del multietapa
    code, r = cli("test", "sim-board", "--no-flash", "--timeout-test", "2s")
    assert code == 0 and r["summary"]["pass"] == 3, r


def test_when_the_pi_stays_down_the_run_keeps_what_already_ran(cli, board, monkeypatch, tmp_path):
    _app(board, [("uno", "[es]", "pass"), ("dos", "[es]", "pass"), ("tres", "[es]", "pass")])
    _flaky_reads(monkeypatch, lambda n: n > 5)              # el menú inicial y 2 lecturas por test
    junit = tmp_path / "r.xml"
    code, r = cli("test", "sim-board", "--no-flash", "--timeout-test", "2s", "--junit", str(junit))
    assert (code, r["error"], r["aborted"]) == (10, "network", True)
    assert [t["name"] for t in r["tests"]] == ["uno", "dos"] and "2 de 3 tests" in r["message"]
    assert ET.parse(junit).getroot().find("testsuite").get("tests") == "2"

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
espbench — CLI para agentes (y humanos) sobre client/espbench_lib.py.

    espbench ls [--all]
    espbench status|who|reserve|release|restart-session <dev>
    espbench events <dev>|--all [--type a,b] [--since A] [--limit N]
    espbench logs <dev> [--since A] [--until X] [--around E] [--before N] [--after N] [--grep re]
                        [--src s] [--max-lines N] [--timeout D] [--for D]
    espbench send <dev> "txt" [--no-enter] [--until X] [--for D] [--timeout D] [--max-lines N]
    espbench flash <dev> [--build-dir B] [--no-encrypt] [--erase] [--verify[=10s]] [--until X] [--timeout D]
    espbench reset <dev> [--bootloader] [--verify[=10s]] [--until X] [--timeout D]

Comunes: --json, --host, --profile, --expect-panic. Con --json, un objeto JSON
por comando en stdout. Exit codes y `error`: docs/specs/agents-cli.md §8.3.
"""
import argparse
import contextlib
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from client import espbench_lib as lib  # noqa: E402
from client.espbench_lib import EspbenchError  # noqa: E402


# ---------- salida ----------

class Out:
    def __init__(self, as_json: bool):
        self.json = as_json

    def log(self, msg: str) -> None:
        if not self.json:
            print(msg, file=sys.stderr, flush=True)

    def emit(self, obj: dict, human=None) -> int:
        error = obj.get("error") if not obj.get("ok", True) else None
        if self.json:
            print(json.dumps(obj, ensure_ascii=False))
        else:
            if human is not None:
                human(obj)
            if error:
                print(f"error: {error}: {obj.get('message') or ''}".rstrip(), file=sys.stderr)
        return lib.exit_code(error)


def _human_lines(r: dict) -> None:
    for line in r.get("lines") or []:
        print(line)
    if r.get("match"):
        print(f"== until: {r['match']}", file=sys.stderr)
    crash = r.get("crash")
    if crash:
        print(f"== {crash.get('type')} {json.dumps(crash.get('detail') or {}, ensure_ascii=False)}", file=sys.stderr)


def _human_devices(r: dict) -> None:
    rows = [("KEY", "SN", "MAC", "TTY", "STATE", "LOCK", "FW")]
    for d in r["devices"]:
        lock = d.get("lock_user") or ""
        if lock and d.get("lock_expires"):
            lock += f" (hasta {d['lock_expires']})"
        fw = " ".join(x for x in (d.get("fw_project"), d.get("fw_version")) if x)
        rows.append(tuple(str(x or "-") for x in (d["key"], d.get("sn"), d.get("mac"), d.get("tty"),
                                                   d.get("state"), lock, fw)))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())


def _human_events(r: dict) -> None:
    for e in r.get("events") or []:
        who = f"[{e['board']}] " if e.get("board") else ""
        detail = json.dumps(e.get("detail") or {}, ensure_ascii=False)
        print(f"{who}{e.get('ts')} {e.get('type'):<9} {detail} {e.get('cursor')}")


def _human_kv(r: dict) -> None:
    for k, v in r.items():
        if k in ("ok",) or v is None or v == [] or v == {}:
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        print(f"{k}: {v}")


# ---------- comandos ----------

def cmd_ls(c: lib.Client, a, out: Out) -> int:
    devs = [lib.summarize_device(d) for d in c.devices() if a.all or d.get("mac")]
    return out.emit({"ok": True, "devices": devs}, _human_devices)


def cmd_status(c: lib.Client, a, out: Out) -> int:
    board = c.resolve(a.dev, need_mac=False)
    r = {"ok": True, "board": board.label}
    if board.info:
        r.update(lib.summarize_device(board.info))
        for k in ("last_flash_ts", "last_flash_user", "last_flash_ok"):
            r[k] = board.info.get(k)
        r["health"] = board.info.get("health")
    else:
        r["state"] = None
    if board.mac or not board.info:
        ev = c.board_events(board.key, limit=10)
        r["session"] = ev.get("session")
        r["events"] = ev.get("events")
    return out.emit(r, _human_kv)


def cmd_who(c: lib.Client, a, out: Out) -> int:
    board = c.resolve(a.dev, write=False, need_mac=False)
    if not board.info:
        raise EspbenchError("not_found", f"no hay placa '{a.dev}' conectada")
    r = {"ok": True, "board": board.label, "tty": board.tty, "lock_user": board.lock_user,
         "lock_expires": board.lock_expires, "reservation": bool(board.lock_expires),
         "mine": bool(board.lock_user) and board.lock_user == c.config.lock_user}
    return out.emit(r, _human_kv)


def cmd_events(c: lib.Client, a, out: Out) -> int:
    if a.all:
        evs = []
        for d in c.devices():
            if not d.get("mac"):
                continue
            key = lib.bare_mac(d["mac"])
            label = d.get("device_key") or d.get("sn") or d["mac"]
            r = c.board_events(key, types=a.type, since=a.since, limit=a.limit)
            evs += [{**e, "board": label} for e in r.get("events") or []]
        evs.sort(key=lambda e: e.get("ts") or "")
        if a.limit:
            evs = evs[-a.limit:]
        return out.emit({"ok": True, "events": evs}, _human_events)
    if not a.dev:
        raise EspbenchError("bad_request", "events: falta la placa (o --all)")
    board = c.resolve(a.dev)
    r = c.board_events(board.key, types=a.type, since=a.since, limit=a.limit)
    return out.emit({"ok": True, "board": board.label, **r}, _human_events)


def _wait_args(a) -> dict:
    return {"timeout_s": lib.parse_duration(a.timeout, "--timeout") if a.timeout else None,
            "for_s": lib.parse_duration(getattr(a, "for_"), "--for") if getattr(a, "for_", None) else None,
            "max_lines": a.max_lines, "expect_panic": a.expect_panic}


def _validate(a) -> None:
    """Todo lo que puede ser bad_request, ANTES de escribir: una escritura que
    salió y después falla por un argumento deja la placa en un estado que el
    agente no pidió (antes `flash --verify=abc` flasheaba y recién ahí fallaba)."""
    for name, value in (("--timeout", getattr(a, "timeout", None)), ("--for", getattr(a, "for_", None)),
                        ("--verify", getattr(a, "verify", None)), ("--ttl", getattr(a, "ttl", None))):
        if value is not None:
            lib.parse_duration(value, name)
    until = getattr(a, "until", None) or ""
    if until.startswith("idle:"):
        lib.parse_duration(until[5:], "idle")
    for name, pattern in (("--until", until[3:] if until.startswith("re:") else None),
                          ("--grep", getattr(a, "grep", None))):
        if pattern:
            try:
                re.compile(pattern)
            except re.error as e:
                raise EspbenchError("bad_request", f"{name}: regex inválida: {e}")


@contextlib.contextmanager
def _after_written(written: dict):
    """Un error después de escribir lleva lo que ya se escribió (sent, cursor, job_id)."""
    try:
        yield
    except EspbenchError as e:
        e.data = {**{k: v for k, v in written.items() if v is not None and k != "ok"}, **e.data}
        raise


def cmd_logs(c: lib.Client, a, out: Out) -> int:
    _validate(a)
    board = c.resolve(a.dev)
    r = c.read_range(board.key, since=a.since, until=a.until, around=a.around, before=a.before, after=a.after,
                     grep=a.grep, src=a.src, **_wait_args(a))
    r["board"] = board.label
    return out.emit(r, _human_lines)


def cmd_send(c: lib.Client, a, out: Out) -> int:
    _validate(a)
    board = c.resolve(a.dev, write=True)
    waiting = bool(a.until or a.for_)
    known = c.crash_snapshot(board.key) if waiting else None
    s = c.send(board, a.text, enter=not a.no_enter)
    r = {"ok": True, "board": board.label, "sent": a.text, "cursor": s.get("cursor")}
    if waiting:
        with _after_written(r):
            if not s.get("cursor"):
                raise EspbenchError("unexpected", "el server no devolvió el cursor del send")
            w = c.read_range(board.key, since=s["cursor"], until=a.until, echo=a.text, idle_needs_output=True,
                             known=known, **_wait_args(a))
        if w.get("start") == r["cursor"]:
            w.pop("start")
        r.update({k: v for k, v in w.items() if k != "board"})
    return out.emit(r, _human_lines)


def _verify_window(a) -> float:
    return lib.parse_duration(a.verify, "--verify") if a.verify is not None else 0.0


def _now_cursor(c: lib.Client, board: lib.Board) -> str:
    return c.board_log(board.key, since="now", max_lines=1)["end"]


def _waits_after_write(a) -> bool:
    return a.verify is not None or bool(a.until)


def _after_write(c: lib.Client, a, out: Out, board: lib.Board, since: str, r: dict, known) -> int:
    """--verify / --until después de un flash o un reset."""
    if not _waits_after_write(a):
        return out.emit(r, _human_kv)
    timeout = lib.parse_duration(a.timeout, "--timeout") if a.timeout else lib.VERIFY_TIMEOUT_S
    with _after_written(r):
        v = c.verify(board, since, window_s=_verify_window(a), until=a.until, timeout_s=timeout,
                     expect_panic=a.expect_panic, max_lines=a.max_lines, known=known)
    if v["ok"] and a.max_lines is None:
        v = {k: x for k, x in v.items() if k not in ("lines", "events")}      # ok: alcanza con el boot
    r["verify"] = v
    if not v["ok"]:
        r.update({"ok": False, "error": v["error"], "message": v.get("message")})

    def human(obj):
        _human_lines(v)
        print(f"== verify: {'ok' if v['ok'] else v['error']} (boot: {v.get('boot') or '-'})", file=sys.stderr)
    return out.emit(r, human)


def cmd_flash(c: lib.Client, a, out: Out) -> int:
    _validate(a)
    board = c.resolve(a.dev, write=True)
    fcfg = c.config.flashcfg
    root = pathlib.Path(".")
    if c.config.flashcfg_path is not None:
        root = c.config.flashcfg_path.parent / (fcfg.get("paths") or {}).get("project_root", ".")
    build_dir = pathlib.Path(a.build_dir)
    if not build_dir.is_absolute():
        build_dir = root / build_dir
    encrypt = bool(fcfg.get("encrypt", True)) if a.encrypt is None else a.encrypt
    erase = bool(fcfg.get("erase", False)) or a.erase
    before = known = None
    if _waits_after_write(a):
        before, known = _now_cursor(c, board), c.crash_snapshot(board.key)
    r = c.flash(board, build_dir.resolve(), chip=str(fcfg.get("chip") or "auto"),
                baud=int(fcfg.get("flash_baud") or 921600), encrypt=encrypt, erase=erase,
                on_line=None if out.json else (lambda l: print(l, file=sys.stderr, flush=True)))
    r["board"] = board.label
    if not _waits_after_write(a):
        return out.emit(r, _human_kv)
    with _after_written(r):
        ready = c.wait_ready(board)
    if ready is None:
        out.log("la placa no volvió a monitoring todavía; verifico igual")
    return _after_write(c, a, out, board, r.get("cursor") or before, r, known)


def cmd_reset(c: lib.Client, a, out: Out) -> int:
    _validate(a)
    if a.bootloader and _waits_after_write(a):
        raise EspbenchError("bad_request", "--bootloader no se combina con --verify/--until")
    board = c.resolve(a.dev, write=True)
    before = known = None
    if _waits_after_write(a):
        before, known = _now_cursor(c, board), c.crash_snapshot(board.key)
    s = c.command(board, "bootloader" if a.bootloader else "reset")
    r = {"ok": True, "board": board.label, "command": s.get("command"), "cursor": s.get("cursor")}
    return _after_write(c, a, out, board, s.get("cursor") or before, r, known)


def cmd_reserve(c: lib.Client, a, out: Out) -> int:
    _validate(a)
    board = c.resolve(a.dev, write=True)
    r = c.reserve(board, int(lib.parse_duration(a.ttl, "--ttl")))
    return out.emit({"ok": True, "board": board.label, "tty": board.tty, "user": r.get("user"),
                     "expires": r.get("expires")}, _human_kv)


def cmd_release(c: lib.Client, a, out: Out) -> int:
    board = c.resolve(a.dev, write=True)
    r = c.release(board)
    return out.emit({"ok": True, "board": board.label, "message": r.get("message")}, _human_kv)


def cmd_restart_session(c: lib.Client, a, out: Out) -> int:
    # El proceso puede estar caído (justo para eso es): alcanza con que la Pi conozca el tty
    board = c.resolve(a.dev, write=False, need_mac=False)
    if not board.tty:
        raise EspbenchError("not_found", f"no hay placa '{a.dev}' en la Pi")
    c.restart_session(board)
    return out.emit({"ok": True, "board": board.label, "tty": board.tty}, _human_kv)


# ---------- argumentos ----------

def build_parser() -> argparse.ArgumentParser:
    # Los comunes valen antes o después del subcomando (SUPPRESS: el del subcomando no pisa al de arriba)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="salida JSON (un objeto)")
    common.add_argument("--host", default=argparse.SUPPRESS, help="host de la Pi (host[:puerto], default 8080)")
    common.add_argument("--profile", default=argparse.SUPPRESS, help="perfil de ~/.config/espbench.json")
    common.add_argument("--expect-panic", action="store_true", default=argparse.SUPPRESS,
                        help="un panic en la ventana es el resultado buscado (exit 0)")

    p = argparse.ArgumentParser(prog="espbench", parents=[common],
                                description="espbench para agentes: placas ESP32 en una Pi (ver client/agent/SKILL.md)")
    sub = p.add_subparsers(dest="cmd", metavar="COMANDO")
    sub.required = True

    def add(name, fn, help_, dev=True):
        sp = sub.add_parser(name, parents=[common], help=help_)
        sp.set_defaults(fn=fn)
        if dev:
            sp.add_argument("dev", help="device_key, SN, MAC o tty")
        return sp

    def waits(sp, timeout_help="espera máxima (default 30s)"):
        sp.add_argument("--until", help="evento (boot, panic, flash...), 're:<regex>', texto, o idle:<dur>")
        sp.add_argument("--timeout", help=timeout_help)
        sp.add_argument("--for", dest="for_", metavar="D", help="ventana fija (firmware que nunca queda idle)")
        sp.add_argument("--max-lines", type=int, help="tope de líneas (default 200; cabeza + cola)")

    sp = add("ls", cmd_ls, "placas de la Pi", dev=False)
    sp.add_argument("--all", action="store_true", help="incluye las que todavía no tienen MAC")

    add("status", cmd_status, "estado, salud y últimos eventos de una placa")
    add("who", cmd_who, "quién tiene el lock / la reserva")

    sp = add("events", cmd_events, "eventos de una placa (o de todas con --all)", dev=False)
    sp.add_argument("dev", nargs="?", help="device_key, SN, MAC o tty")
    sp.add_argument("--all", action="store_true", help="todas las placas de /api/devices")
    sp.add_argument("--type", help="tipos separados por coma (boot,panic,flash,send,...)")
    sp.add_argument("--since", help="anchor (session, boot~1, 5m, 16:02, c:<cursor>)")
    sp.add_argument("--limit", type=int, help="los últimos N (default 50)")

    sp = add("logs", cmd_logs, "rango del log (con espera opcional)")
    sp.add_argument("--since", help="anchor de inicio (default session)")
    sp.add_argument("--around", help="evento: del boot anterior al siguiente")
    sp.add_argument("--before", type=int, help="con --around: líneas antes")
    sp.add_argument("--after", type=int, help="con --around: líneas después")
    sp.add_argument("--grep", help="regex sobre lo que se muestra")
    sp.add_argument("--src", choices=("serial", "taglog", "all"), help="origen de las líneas")
    waits(sp)

    sp = add("send", cmd_send, "manda texto a la consola serie")
    sp.add_argument("text")
    sp.add_argument("--no-enter", action="store_true", help="sin Enter al final")
    waits(sp)

    sp = add("flash", cmd_flash, "flashea el build dir (flasher_args.json)")
    sp.add_argument("--build-dir", default="build", help="default build (relativo al project_root)")
    sp.add_argument("--no-encrypt", dest="encrypt", action="store_false", default=None)
    sp.add_argument("--erase", action="store_true")
    sp.add_argument("--verify", nargs="?", const="10s", metavar="D",
                    help="espera el primer boot y una ventana de asentamiento D (default 10s)")
    sp.add_argument("--until", help="además, espera X después del boot")
    sp.add_argument("--timeout", help="espera máxima del boot / until (default 60s)")
    sp.add_argument("--max-lines", type=int)

    sp = add("reset", cmd_reset, "reset (o bootloader) por el monitor")
    sp.add_argument("--bootloader", action="store_true", help="reset a modo download")
    sp.add_argument("--verify", nargs="?", const="10s", metavar="D")
    sp.add_argument("--until")
    sp.add_argument("--timeout")
    sp.add_argument("--max-lines", type=int)

    sp = add("reserve", cmd_reserve, "reserva la placa (bloquea send/reset ajenos)")
    sp.add_argument("--ttl", default="30m", help="vencimiento (default 30m)")
    add("release", cmd_release, "suelta la reserva / el lock")
    add("restart-session", cmd_restart_session, "relanza el proceso de la placa en la Pi (devremote --reset)")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    as_json = "--json" in (argv if argv is not None else sys.argv[1:])
    try:
        a = parser.parse_args(argv)
    except SystemExit as e:
        if e.code in (0, None):
            return 0
        if as_json:
            print(json.dumps({"ok": False, "error": "bad_request", "message": "argumentos inválidos (ver --help)"}))
        return 1
    a.json = getattr(a, "json", False)
    a.expect_panic = getattr(a, "expect_panic", False)
    out = Out(a.json)
    try:
        cfg = lib.Config.load(host=getattr(a, "host", None), profile=getattr(a, "profile", None),
                              device=getattr(a, "dev", None))
        client = lib.Client(cfg, log=out.log)
        return a.fn(client, a, out)
    except EspbenchError as e:
        return out.emit(e.to_dict())
    except KeyboardInterrupt:
        return 130
    except Exception as e:      # el contrato también vale para lo imprevisto
        return out.emit({"ok": False, "error": "unexpected", "message": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    sys.exit(main())

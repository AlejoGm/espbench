#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
espbench — CLI para agentes (y humanos) sobre client/espbench_lib.py.

    espbench benches
    espbench ls [--all] [--bench B] [--where cat=valor ...] [--free]
    espbench pick [--where cat=valor ...] [--reserve [--ttl 30m]]
    espbench note <dev> "texto" | --clear
    espbench set <dev> chip=esp32-s3 conectividad+=lte uso-=ci estado=
    espbench props [add|rm <categoria> <valor> [--label L] [--desc D] [--warn] [--exclude-pick]]
    espbench status|who|reserve|release|restart-session <dev>
    espbench events <dev>|--all [--type a,b] [--since A] [--limit N]
    espbench logs <dev> [--since A] [--until X] [--around E] [--before N] [--after N] [--grep re]
                        [--src s] [--max-lines N] [--timeout D] [--for D]
    espbench send <dev> "txt" [--no-enter] [--until X] [--for D] [--timeout D] [--max-lines N]
    espbench flash <dev> [--build-dir B] [--no-encrypt] [--erase] [--verify[=10s]] [--until X] [--timeout D]
    espbench reset <dev> [--bootloader] [--verify[=10s]] [--until X] [--timeout D]

Comunes: --json, --host, --profile, --bench, --expect-panic. Con --json, un objeto
JSON por comando en stdout. Exit codes y `error`: docs/specs/agents-cli.md §8.3.

Sin host (--host, ESPBENCH_HOST, perfil, .flashcfg.json) los benches se encuentran
solos (client/benches.py): `ls` lista las placas de todos y <dev> se busca en todos
(`<dev>@<bench>` o --bench para elegir uno).
"""
import argparse
import contextlib
import dataclasses
import json
import pathlib
import re
import sys
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from client import espbench_lib as lib  # noqa: E402
from client.espbench_lib import EspbenchError  # noqa: E402


# ---------- salida ----------

class Out:
    def __init__(self, as_json: bool):
        self.json = as_json
        self.extra = {}         # va en todo objeto (discovery: el bench donde está la placa)

    def log(self, msg: str) -> None:
        if not self.json:
            print(msg, file=sys.stderr, flush=True)

    def emit(self, obj: dict, human=None) -> int:
        error = obj.get("error") if not obj.get("ok", True) else None
        if self.extra:
            obj = {**obj, **{k: v for k, v in self.extra.items() if k not in obj}}
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


def _table(rows: list) -> None:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())


def _human_devices(r: dict) -> None:
    multi = any("bench" in d for d in r["devices"])
    rows = [(("BENCH",) if multi else ()) + ("KEY", "SN", "MAC", "TTY", "STATE", "LIBRE", "LOCK", "FW", "PROPS",
                                              "NOTA")]
    for d in r["devices"]:
        lock = d.get("lock_user") or ""
        if lock and d.get("lock_expires"):
            lock += f" (hasta {d['lock_expires']})"
        fw = " ".join(x for x in (d.get("fw_project"), d.get("fw_version")) if x)
        props = _props_text(d.get("props"))
        note = d.get("note") or ""
        if note:
            note = (note if len(note) <= 40 else note[:39] + "…") + (f" ({d['note_by']})" if d.get("note_by") else "")
        libre = "sí" if d.get("available") else ("no (estado)" if d.get("avoid") else "no")
        cols = ((d.get("bench"),) if multi else ()) + (d["key"], d.get("sn"), d.get("mac"), d.get("tty"),
                                                      d.get("state"), libre, lock, fw, props, note)
        rows.append(tuple(str(x or "-") for x in cols))
    _table(rows)
    for e in r.get("errors") or []:
        print(f"bench {e['bench']}: {e['error']}", file=sys.stderr)


def _props_text(props) -> str:
    return " ".join(f"{k}={','.join(v) if isinstance(v, list) else v}" for k, v in (props or {}).items())


def _human_benches(r: dict) -> None:
    rows = [("BENCH", "HOST", "VERSION", "AUTH", "PLACAS", "LIBRES", "")]
    for b in r["benches"]:
        note = "viejo: ignorado" if not b["supported"] else (b["error"] or (
            "sin notas/propiedades: actualizar" if b.get("props") is False else ""))
        rows.append((b["name"], b["host"], b["version"] or "-", {True: "sí", False: "no"}.get(b["auth"], "-"),
                     str(b["boards"]), str(b["available"]), note))
    _table(rows)


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

def cmd_benches(cfg: lib.Config, a, out: Out) -> int:
    """Siempre escanea (y refresca la cache del discovery)."""
    found = lib.Benches(only=None)
    found.load(fresh=True)
    bs = []
    for b in found.found:
        if getattr(a, "bench", None) not in (None, b.name):
            continue
        props = exclude = None
        if not b.legacy and b.ok:
            bc = lib.Client(lib.config_for_bench(cfg, b), log=out.log)
            try:
                bc.properties()
                props, exclude = True, bc.excluded()
            except EspbenchError as e:
                props = False if e.error == "not_found" else None
        bs.append(lib.bench_summary(b, cfg.lock_user, exclude, props))
    return out.emit({"ok": True, "benches": bs}, _human_benches)


def _all_boards(c: lib.Client, a, with_unknown: bool = False):
    """(summaries, errors) de un bench (host configurado) o de todos (a.multi). Cada
    summary lleva `bench` si vino del discovery."""
    devs, errors = [], []
    for b, bc in (a.multi if a.multi is not None else [(None, c)]):
        if b is not None and not b.ok:
            errors.append({"bench": b.name, "error": b.error})
        for d in (b.devices if b is not None else bc.devices()):
            if with_unknown or d.get("mac"):
                devs.append({"bench": b.name, **bc.summarize(d)} if b is not None else bc.summarize(d))
    return devs, errors


def _client_of(c: lib.Client, a, summary: dict) -> lib.Client:
    if a.multi is None:
        return c
    return next(bc for b, bc in a.multi if b.name == summary.get("bench"))


def cmd_ls(c: lib.Client, a, out: Out) -> int:
    where = _where_args(c, a)
    devs, errors = _all_boards(c, a, with_unknown=a.all)
    devs = [d for d in devs if lib.matches_where(d, where) and (not a.free or d["available"])]
    r = {"ok": True, "devices": devs}
    if errors:
        r["errors"] = errors
    return out.emit(r, _human_devices)


def _clients(c: lib.Client, a) -> list:
    return [bc for _, bc in a.multi] if a.multi is not None else [c]


def _where_args(c: lib.Client, a) -> list:
    """--where cat=valor (repetibles, AND), validados contra los catálogos (unión si hay varios benches)."""
    where = lib.parse_where(getattr(a, "where", None))
    if where:
        cats = []
        for bc in _clients(c, a):
            try:
                cats.append(bc.properties())
            except EspbenchError:
                pass            # bench sin catálogo: se filtra igual
        if cats:
            lib.check_props(where, lib.merge_categories(cats))
    return where


def cmd_pick(c: lib.Client, a, out: Out) -> int:
    """La primera placa libre que cumple --where (sin estado no-tocar/roto, sin boot loop, sin
    nota primero), en cualquier bench. --reserve: la reserva; si otro la toma en el medio, la siguiente."""
    _validate(a)
    where = _where_args(c, a)
    if a.reserve:
        c.config.require_creds("pick --reserve")
    devs, errors = _all_boards(c, a)
    skipped = []
    for s in lib.pick_order(devs, where, me=c.config.lock_user, include_mine=a.include_mine):
        r = {"ok": True, "board": s["key"], **s, "reserved": False}
        if not a.reserve:
            return out.emit(r, _human_kv)
        bc = _client_of(c, a, s)
        try:
            board = bc.resolve(s["mac"], write=True)
            res = bc.reserve(board, int(lib.parse_duration(a.ttl, "--ttl")))
        except EspbenchError as e:
            # Un problema de esa placa o de ese bench (otro la tomó, token de la API de ese
            # bench, red): la siguiente. Lo demás (sin lock_user/lock_token...) corta.
            if e.error not in ("locked", "busy", "device_changed", "not_found", "token_mismatch", "auth",
                               "auth_config", "network", "reservation_lost"):
                e.data = {**e.data, "bench": s.get("bench"), "board": s["key"]}
                raise
            skipped.append({"board": s["key"], "bench": s.get("bench"), "error": e.error, "message": e.message})
            continue
        r.update(reserved=True, lock_user=res.get("user"), lock_expires=res.get("expires"), available=True)
        if skipped:
            r["skipped"] = skipped
        return out.emit(r, _human_kv)
    what = " que cumpla " + " ".join(f"{k}={v or ''}" for k, v in where) if where else ""
    matching = sum(1 for d in devs if lib.matches_where(d, where))
    raise EspbenchError("not_found", f"ninguna placa libre{what} ({len(devs)} placas, {matching} cumplen el filtro, "
                                     f"{len(skipped)} tomadas en el medio): `espbench ls --json` para ver por qué",
                        data={"skipped": skipped} if skipped else None)


def cmd_note(c: lib.Client, a, out: Out) -> int:
    if a.clear == (a.text is not None):
        raise EspbenchError("bad_request", 'note: un texto o --clear (espbench note <dev> "testeando, no tocar")')
    if a.text is not None and not a.text.strip():
        raise EspbenchError("bad_request", "note: texto vacío (para borrarla: --clear)")
    c.require_meta("notas")
    board = c.resolve(a.dev, need_mac=False)
    r = c.set_meta(board, note="" if a.clear else a.text)
    return out.emit({"ok": True, "board": board.label, "note": r.get("note"), "note_by": r.get("note_by"),
                     "note_at": r.get("note_at")}, _human_kv)


def cmd_set(c: lib.Client, a, out: Out) -> int:
    props, add, remove = lib.parse_set_ops(a.ops)
    board = c.resolve(a.dev, need_mac=False)
    before = dict(board.info.get("props") or {})
    if not (props or add or remove):        # solo mirar
        return out.emit({"ok": True, "board": board.label, "props": before}, _human_kv)
    cats = c.require_meta("propiedades")
    pairs = [(k, v) for k, vs in list(props.items()) + list(add.items())
             for v in (vs if isinstance(vs, list) else [vs]) if v is not None] + [(k, None) for k in remove]
    lib.check_props(pairs, cats, bench=out.extra.get("bench"))
    fields = {k: v for k, v in (("props", props), ("props_add", add), ("props_remove", remove)) if v}
    r = c.set_meta(board, **fields)
    after = dict(r.get("props") or {})
    changes = {k: {"from": before.get(k), "to": after.get(k)} for k in set(before) | set(after)
               if before.get(k) != after.get(k)}
    return out.emit({"ok": True, "board": board.label, "props": after, "changes": changes}, _human_kv)


def _human_props(r: dict) -> None:
    if "value" in r:
        print(f"{r['category']}={r['value']['id']}")
        return
    multi = len(r.get("benches") or []) > 1
    for cat in r.get("categories") or []:
        print(f"{cat['id']} ({'varios' if cat.get('multi') else 'uno'}):")
        for v in cat.get("values") or []:
            flags = " [no pick]" if v.get("exclude_pick") else (" [!]" if v.get("warn") else "")
            where = f"  [{', '.join(v.get('benches') or [])}]" if multi else ""
            print(f"  {v['id']:<14} {v.get('desc') or ''}{flags}{where}")


def cmd_props(c: lib.Client, a, out: Out) -> int:
    """Sin acción: el catálogo (con varios benches, la unión, y cada valor con `benches`).
    add/rm: el catálogo es de cada bench; con varios, --bench."""
    if not a.action:
        if a.multi is None:
            return out.emit({"ok": True, "categories": c.require_meta("propiedades")}, _human_props)
        lists, names, errors = [], [], []
        for b, bc in a.multi:
            try:
                lists.append(bc.require_meta("propiedades"))
                names.append(b.name)
            except EspbenchError as e:
                errors.append({"bench": b.name, "error": e.error, "message": e.message})
        r = {"ok": True, "categories": lib.merge_categories(lists, names), "benches": names}
        if errors:
            r["errors"] = errors
        return out.emit(r, _human_props)
    if a.action not in ("add", "rm") or len(a.args) != 2:
        raise EspbenchError("bad_request", "props add|rm <categoria> <valor>")
    if a.multi is not None:
        if len(a.multi) != 1:
            names = ", ".join(b.name for b, _ in a.multi) or "ninguno"
            raise EspbenchError("bad_request", f"props {a.action}: el catálogo es de cada bench y hay varios "
                                               f"({names}): elegí uno con --bench <bench>")
        b, c = a.multi[0]
        out.extra["bench"] = b.name
    c.require_meta("propiedades")
    cat, value = a.args
    if a.action == "rm":
        c.request("DELETE", f"/api/properties/{urllib.parse.quote(cat, safe='')}/values/"
                            f"{urllib.parse.quote(value, safe='')}")
        return out.emit({"ok": True, "category": cat, "removed": value}, _human_kv)
    body = {"id": value, "label": a.label, "desc": a.desc, "warn": a.warn, "exclude_pick": a.exclude_pick}
    r = c.request("POST", f"/api/properties/{urllib.parse.quote(cat, safe='')}/values", body=body) or {}
    return out.emit({"ok": True, "category": cat, "value": r.get("value")}, _human_props)


def cmd_status(c: lib.Client, a, out: Out) -> int:
    board = c.resolve(a.dev, need_mac=False)
    r = {"ok": True, "board": board.label}
    if board.info:
        r.update(c.summarize(board.info))
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
        evs, errors = [], []
        for b, bc in (a.multi if a.multi is not None else [(None, c)]):
            try:
                for d in (b.devices if b is not None else bc.devices()):
                    if not d.get("mac"):
                        continue
                    key = lib.bare_mac(d["mac"])
                    label = d.get("device_key") or d.get("sn") or d["mac"]
                    r = bc.board_events(key, types=a.type, since=a.since, limit=a.limit)
                    extra = {"bench": b.name} if b is not None else {}
                    evs += [{**e, "board": label, **extra} for e in r.get("events") or []]
            except EspbenchError as e:
                if b is None:
                    raise
                errors.append({"bench": b.name, "error": e.error, "message": e.message})   # un bench no frena al resto
        evs.sort(key=lambda e: e.get("ts") or "")
        if a.limit:
            evs = evs[-a.limit:]
        r = {"ok": True, "events": evs}
        if errors:
            r["errors"] = errors
        return out.emit(r, _human_events)
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


def _discover(cfg: lib.Config, a, out: Out, found: "lib.Benches") -> None:
    """Sin host: `ls` / `events --all` van a todos los benches (a.multi); un
    comando con <dev> va al bench donde está la placa (cfg.host) y la nombra por
    su MAC (lo que se pidió puede ser `<dev>@<bench>`)."""
    if a.fn in (cmd_ls, cmd_pick, cmd_props) or (a.fn is cmd_events and getattr(a, "all", False)):
        a.multi = [(b, lib.Client(lib.config_for_bench(cfg, b), log=out.log)) for b in found.list()]
        if a.fn is cmd_props and not a.multi:
            raise EspbenchError("not_found", "no se encontró ningún bench (`espbench benches`)")
        return
    if not getattr(a, "dev", None):
        return
    bench, d = found.locate(a.dev)
    cfg.token = lib.config_for_bench(cfg, bench).token
    cfg.host = bench.url
    cfg.sources["host"] = f"bench:{bench.name}"
    a.dev = d.get("mac") or d.get("tty_name")
    out.extra["bench"] = bench.name


# ---------- argumentos ----------

def build_parser() -> argparse.ArgumentParser:
    # Los comunes valen antes o después del subcomando (SUPPRESS: el del subcomando no pisa al de arriba)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="salida JSON (un objeto)")
    common.add_argument("--host", default=argparse.SUPPRESS, help="host de la Pi (host[:puerto], default 8080)")
    common.add_argument("--profile", default=argparse.SUPPRESS, help="perfil de ~/.config/espbench.json")
    common.add_argument("--bench", default=argparse.SUPPRESS,
                        help="solo ese bench (discovery por Tailscale/config, ignora ESPBENCH_HOST y el perfil)")
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
            sp.add_argument("dev", help="device_key, SN, MAC o tty (sin host: también <dev>@<bench>)")
        return sp

    def waits(sp, timeout_help="espera máxima (default 30s)"):
        sp.add_argument("--until", help="evento (boot, panic, flash...), 're:<regex>', texto, o idle:<dur>")
        sp.add_argument("--timeout", help=timeout_help)
        sp.add_argument("--for", dest="for_", metavar="D", help="ventana fija (firmware que nunca queda idle)")
        sp.add_argument("--max-lines", type=int, help="tope de líneas (default 200; cabeza + cola)")

    add("benches", cmd_benches, "benches encontrados (Tailscale + ~/.config/espbench-benches.json)", dev=False)

    sp = add("ls", cmd_ls, "placas de la Pi (sin host: de todos los benches)", dev=False)
    sp.add_argument("--all", action="store_true", help="incluye las que todavía no tienen MAC")
    sp.add_argument("--where", action="append", metavar="CAT=VALOR",
                    help="filtro por propiedad (repetible: todas), ej. chip=esp32-s3; 'estado=' = sin estado")
    sp.add_argument("--free", action="store_true", help="solo disponibles (sin lock ajeno, sin estado no-tocar/roto)")

    sp = add("pick", cmd_pick, "la primera placa libre que cumple --where, en cualquier bench", dev=False)
    sp.add_argument("--where", action="append", metavar="CAT=VALOR", help="propiedad requerida (repetible: todas)")
    sp.add_argument("--reserve", action="store_true", help="además la reserva (si otro la toma, la siguiente)")
    sp.add_argument("--include-mine", action="store_true", help="también las que ya tengo reservadas")
    sp.add_argument("--ttl", default="30m", help="vencimiento de la reserva (default 30m)")

    sp = add("note", cmd_note, "nota de la placa (aviso: \"testeando, no tocar\")")
    sp.add_argument("text", nargs="?", help="texto (hasta 200 caracteres)")
    sp.add_argument("--clear", action="store_true", help="borra la nota")

    sp = add("set", cmd_set, "propiedades de la placa: cat=valor, cat+=valor, cat-=valor, cat= (quitar)")
    sp.add_argument("ops", nargs="*", metavar="CAT=VALOR")

    sp = add("props", cmd_props, "catálogo de propiedades; add/rm de valores", dev=False)
    sp.add_argument("action", nargs="?", metavar="add|rm")
    sp.add_argument("args", nargs="*", metavar="CATEGORIA VALOR")
    sp.add_argument("--label")
    sp.add_argument("--desc")
    sp.add_argument("--warn", action="store_true", help="(estado) estilo de advertencia")
    sp.add_argument("--exclude-pick", action="store_true", help="(estado) pick / ls --free no la eligen")

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
    argv = list(argv if argv is not None else sys.argv[1:])
    as_json = "--json" in argv
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
    a.multi = None
    out = Out(a.json)
    try:
        cfg = lib.Config.load(host=getattr(a, "host", None), profile=getattr(a, "profile", None),
                              device=getattr(a, "dev", None))
        if a.fn is cmd_benches:
            return cmd_benches(cfg, a, out)
        bench = getattr(a, "bench", None)
        if bench is not None:
            if getattr(a, "host", None):
                raise EspbenchError("bad_request", "--host y --bench no van juntos")
            cfg.host = None
        if cfg.discovery:
            _discover(cfg, a, out, lib.Benches(only=bench))
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

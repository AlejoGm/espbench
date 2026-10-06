"""
board_meta.py — nota y propiedades por placa (por MAC en devices.json, DevicesFile).

- Nota: texto libre corto ("testeando, no tocar"). Es un aviso, no un lock.
- Propiedades: categorías **fijas** (CATEGORIES, acá en el código) con valores
  **editables por bench** (properties.json en ESP_BASE, sembrado con los valores
  iniciales). Se agregan valores a una categoría existente, nunca categorías; un
  valor se borra solo si ninguna placa lo usa. Por placa: `props` =
  {"chip": "esp32-s3", "conectividad": ["wifi", "lte"]} (`multi`: lista).
  `exclude_pick` (en `estado`): `espbench pick` / `ls --free` no eligen esa placa;
  `warn`: estilo de advertencia en el dashboard. El server no bloquea nada por
  una propiedad.
"""
import contextlib
import copy
import difflib
import fcntl
import json
import os
import re
import shutil
import tempfile
import unicodedata
from typing import Dict, List, Optional

from server import paths, taglog

TAG = "board_meta"

NOTE_MAX = 200
USER_MAX = 64
VALUE_MAX = 24
LABEL_MAX = 40
DESC_MAX = 120
VALUE_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")


def _v(id_: str, desc: str, label: Optional[str] = None, warn: bool = False) -> dict:
    out = {"id": id_, "label": label or id_, "desc": desc}
    if warn:
        out.update(warn=True, exclude_pick=True)
    return out


# Categorías fijas (no se agregan desde la API). `values`: el set inicial de cada bench.
CATEGORIES = [
    {"id": "estado", "label": "estado", "multi": False, "values": [
        _v("no-tocar", "Nadie la usa sin preguntar (ni agentes)", "no tocar", warn=True),
        _v("testeando", "Alguien está probando algo en esta placa"),
        _v("roto", "Hardware o conexión con problemas", warn=True)]},
    {"id": "uso", "label": "uso", "multi": True, "values": [
        _v("agentes", "Libre para que la usen agentes"),
        _v("ci", "La usa la integración continua", "CI"),
        _v("demo", "Reservada para demos")]},
    {"id": "chip", "label": "chip", "multi": False, "values": [
        _v("esp32", "ESP32 clásico", "ESP32"), _v("esp32-s3", "ESP32-S3", "ESP32-S3"),
        _v("esp32-c3", "ESP32-C3", "ESP32-C3")]},
    {"id": "conectividad", "label": "conectividad", "multi": True, "values": [
        _v("wifi", "WiFi disponible", "WiFi"), _v("lte", "Módem LTE con SIM", "LTE"),
        _v("ble", "Bluetooth LE", "BLE")]},
]
CATEGORY_IDS = [c["id"] for c in CATEGORIES]
# Solo en estas categorías un valor puede ser warn / exclude_pick.
FLAG_CATEGORIES = ("estado",)


class MetaError(ValueError):
    """Pedido inválido (400 bad_request)."""


class InUseError(ValueError):
    """Valor en uso por alguna placa (409)."""


class NotFoundError(LookupError):
    """Valor que no existe (404)."""


# ---------- texto ----------

def _has_control(s: str) -> bool:
    """Caracteres de control y de formato Unicode (Cf: bidi override, ZWSP, ZWJ...): con
    ellos una nota puede mostrarse distinta de lo que dice (texto invertido, invisible)."""
    return any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f or unicodedata.category(c) == "Cf" for c in s)


def _clean_text(text, what: str, max_len: int) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        raise MetaError(f"{what} tiene que ser texto")
    text = text.strip()
    if len(text) > max_len:
        raise MetaError(f"{what} tiene más de {max_len} caracteres")
    if _has_control(text):
        raise MetaError(f"{what} no puede tener caracteres de control ni de formato (saltos de línea, tabs, "
                        "bidi, espacios de ancho cero)")
    return text


def clean_note(text) -> str:
    """Nota normalizada ("" = borrar). MetaError si es larga o tiene caracteres de control."""
    return _clean_text(text, "la nota", NOTE_MAX)


def clean_user(user) -> Optional[str]:
    return _clean_text(None if user is None else str(user), "user", USER_MAX) or None


# ---------- catálogo (properties.json) ----------

def _seed() -> Dict[str, list]:
    return {c["id"]: copy.deepcopy(c["values"]) for c in CATEGORIES}


def _read_values() -> Dict[str, list]:
    """Valores por categoría: los del archivo, o el set inicial si no existe. Una
    categoría que falta en el archivo (agregada al código después) toma su set inicial;
    una que ya no está en el código se ignora. Sin el archivo en meta/, el de la ruta
    vieja (/opt/esp/properties.json): la primera escritura lo migra a meta/."""
    path = paths.properties_file()
    if not path.exists() and paths.legacy_properties_file().exists():
        path = paths.legacy_properties_file()
    values = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        values = data.get("values") if isinstance(data, dict) else None
        if values is not None and not isinstance(values, dict):
            raise ValueError("values no es un objeto")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        _quarantine(path, e)
    seed = _seed()
    if not isinstance(values, dict):
        return seed
    out = {}
    for cat in CATEGORY_IDS:
        vals = values.get(cat)
        out[cat] = [v for v in vals if isinstance(v, dict) and VALUE_RE.fullmatch(str(v.get("id") or ""))] \
            if isinstance(vals, list) else seed[cat]
    return out


_quarantined = set()


def _quarantine(path, error) -> None:
    """properties.json ilegible: se loguea y se guarda una copia `.bad` (una vez por
    archivo), así la próxima escritura (que lo reemplaza con el set inicial + el valor
    nuevo) no pierde lo que había sin dejar rastro."""
    key = str(path)
    if key in _quarantined:
        return
    _quarantined.add(key)
    taglog.error(TAG, f"{path} ilegible ({error}): uso los valores iniciales; copia en {path}.bad")
    try:
        shutil.copyfile(str(path), str(path) + ".bad")
    except OSError as e:
        taglog.error(TAG, f"no se pudo copiar {path} a .bad: {e}")


def catalog() -> List[dict]:
    """Categorías con sus valores: [{id, label, multi, values: [{id, label, desc, warn?, exclude_pick?}]}]."""
    values = _read_values()
    return [{"id": c["id"], "label": c["label"], "multi": c["multi"], "values": values[c["id"]]}
            for c in CATEGORIES]


@contextlib.contextmanager
def _locked():
    path = paths.properties_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.with_name(path.name + ".lck")), os.O_RDWR | os.O_CREAT, 0o666)
    try:
        try:
            os.fchmod(fd, 0o666)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write_values(values: Dict[str, list]) -> None:
    path = paths.properties_file()
    _quarantined.discard(str(path))
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".properties.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"values": values}, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o666)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _category(cat: str) -> dict:
    for c in CATEGORIES:
        if c["id"] == cat:
            return c
    raise MetaError(f"categoría '{cat}' no existe. Categorías: {', '.join(CATEGORY_IDS)}")


def add_value(cat: str, value: str, label=None, desc=None, warn: bool = False, exclude_pick: bool = False) -> dict:
    """Agrega un valor a una categoría existente. MetaError si es inválido o ya existe."""
    _category(cat)
    vid = str(value or "").strip().lower()
    if not VALUE_RE.fullmatch(vid) or len(vid) > VALUE_MAX:
        raise MetaError(f"valor inválido: {value!r} (minúsculas, números, '.', '_', '-'; hasta {VALUE_MAX})")
    if (warn or exclude_pick) and cat not in FLAG_CATEGORIES:
        raise MetaError(f"warn / exclude_pick solo en: {', '.join(FLAG_CATEGORIES)}")
    entry = {"id": vid, "label": _clean_text(label, "label", LABEL_MAX) or vid,
             "desc": _clean_text(desc, "desc", DESC_MAX)}
    if warn:
        entry["warn"] = True
    if exclude_pick:
        entry["exclude_pick"] = True
    with _locked():
        values = _read_values()
        if any(v["id"] == vid for v in values[cat]):
            raise MetaError(f"'{vid}' ya existe en {cat}")
        values[cat].append(entry)
        _write_values(values)
    return entry


def remove_value(cat: str, value: str, in_use) -> None:
    """Borra un valor si ninguna placa lo usa. `in_use(cat, value)` → lista de placas que lo
    tienen (se llama con el lock del catálogo tomado). InUseError / NotFoundError."""
    _category(cat)
    with _locked():
        values = _read_values()
        if not any(v["id"] == value for v in values[cat]):
            raise NotFoundError(f"'{value}' no es un valor de {cat}")
        users = in_use(cat, value)
        if users:
            raise InUseError(f"'{cat}={value}' lo usan: {', '.join(users)}. Sacáselo primero")
        values[cat] = [v for v in values[cat] if v["id"] != value]
        _write_values(values)


# ---------- props de una placa ----------

def suggest(value: str, ids: List[str]) -> Optional[str]:
    m = difflib.get_close_matches(value, ids, n=1, cutoff=0.5)
    return m[0] if m else None


def _values_list(raw, cat: str) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)) or not all(isinstance(x, str) for x in raw):
        raise MetaError(f"{cat}: un valor o una lista de valores")
    out = []
    for x in raw:
        x = x.strip().lower()
        if x and x not in out:
            out.append(x)
    return out


def _check_values(cat: str, vals: List[str], cat_values: Dict[str, list]) -> None:
    ids = [v["id"] for v in cat_values[cat]]
    for x in vals:
        if x not in ids:
            near = suggest(x, ids)
            raise MetaError(f"{cat}: '{x}' no es un valor válido" + (f" (¿'{near}'?)" if near else "") +
                            f". Válidos: {', '.join(ids)} (o agregalo al catálogo)")


def plan_props(props=None, add=None, remove=None) -> dict:
    """Valida un pedido de cambios y lo deja en {cat: [(kind, [vals]), ...]}, kind "set" /
    "add" / "remove", aplicados en ese orden (`conectividad+=ble conectividad-=wifi` en un
    mismo pedido). props: {cat: valor | [valores] | null/""/[] (= quitar)}; add/remove:
    {cat: valor | [valores]}. Los valores a poner tienen que estar en el catálogo; quitar
    vale para cualquiera."""
    ops = {}
    cat_values = _read_values()
    for kind, src in (("set", props), ("add", add), ("remove", remove)):
        if src is None:
            continue
        if not isinstance(src, dict):
            raise MetaError("props / props_add / props_remove van como {categoría: valor(es)}")
        for cat, raw in src.items():
            c = _category(cat)
            vals = _values_list(raw, cat)
            if kind != "remove":
                _check_values(cat, vals, cat_values)
            if kind == "set" and not c["multi"] and len(vals) > 1:
                raise MetaError(f"{cat} admite un solo valor")
            if kind == "add" and not c["multi"]:
                if len(vals) > 1:
                    raise MetaError(f"{cat} admite un solo valor")
                if "set" in [k for k, _ in ops.get(cat, [])]:
                    raise MetaError(f"{cat}: un solo valor, y aparece dos veces en el pedido")
                kind_ = "set"
            else:
                kind_ = kind
            ops.setdefault(cat, []).append((kind_, vals))
    return ops


def apply_props(current: dict, ops: dict) -> tuple:
    """(props nuevas, cambios {cat: {"from", "to"}}) aplicando `ops` de plan_props."""
    new = {k: (list(v) if isinstance(v, list) else v) for k, v in (current or {}).items()}
    multi = {c["id"]: c["multi"] for c in CATEGORIES}
    changes = {}
    for cat, steps in ops.items():
        old = new.get(cat)
        res = old if isinstance(old, list) else ([old] if old else [])
        for kind, vals in steps:
            if kind == "set":
                res = list(vals)
            elif kind == "add":
                res = res + [v for v in vals if v not in res]
            else:
                res = [v for v in res if v not in vals]
        value = (res if multi[cat] else (res[0] if res else None)) if res else None
        if value is None:
            new.pop(cat, None)
        else:
            new[cat] = value
        if value != (old or None):
            changes[cat] = {"from": old, "to": value}
    return new, changes


def excluded_values(cat_list: List[dict]) -> Dict[str, List[str]]:
    """{categoría: [valores con exclude_pick]} (lo que pick / --free no eligen)."""
    return {c["id"]: [v["id"] for v in c["values"] if v.get("exclude_pick")] for c in cat_list
            if any(v.get("exclude_pick") for v in c["values"])}

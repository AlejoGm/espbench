"""
board_meta.py — nota y tags por placa (los guarda DevicesFile en devices.json).

- Nota: texto libre corto ("testeando, no tocar"). Es un aviso, no un lock.
- Tags: fijos, del catálogo `tags.json` (al lado de este archivo; se lee en cada
  pedido). Un tag fuera del catálogo no se puede agregar; uno que se sacó del
  catálogo y una placa todavía tiene se puede quitar. `kind: "warn"`: el
  dashboard lo marca y `espbench pick` / `ls --free` no eligen esa placa. El
  server no bloquea nada por un tag.
"""
import difflib
import json
import pathlib
import re
from typing import List, Optional

CATALOG_FILE = pathlib.Path(__file__).with_name("tags.json")
NOTE_MAX = 200
USER_MAX = 64
TAG_MAX = 24
TAGS_PER_BOARD = 12
TAG_RE = re.compile(r"[a-z0-9][a-z0-9._:-]*")


class MetaError(ValueError):
    """Pedido inválido (400 bad_request)."""


def catalog(path: Optional[pathlib.Path] = None) -> List[dict]:
    """Tags del catálogo: [{id, label, group, desc, kind?, color?}]. Las entradas
    inválidas se saltean (un error de tipeo en tags.json no tira el dashboard)."""
    try:
        data = json.loads(pathlib.Path(path or CATALOG_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out, seen = [], set()
    entries = data.get("tags") if isinstance(data, dict) else None
    for t in entries if isinstance(entries, list) else []:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        if not TAG_RE.fullmatch(tid) or len(tid) > TAG_MAX or tid in seen:
            continue
        seen.add(tid)
        entry = {"id": tid, "label": str(t.get("label") or tid), "group": str(t.get("group") or "otros"),
                 "desc": str(t.get("desc") or "")}
        for k in ("kind", "color"):
            if t.get(k):
                entry[k] = str(t[k])
        out.append(entry)
    return out


def _has_control(s: str) -> bool:
    return any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f for c in s)


def clean_note(text) -> str:
    """Nota normalizada ("" = borrar). MetaError si es larga o tiene caracteres de control."""
    if text is None:
        return ""
    if not isinstance(text, str):
        raise MetaError("note tiene que ser texto")
    text = text.strip()
    if len(text) > NOTE_MAX:
        raise MetaError(f"la nota tiene más de {NOTE_MAX} caracteres")
    if _has_control(text):
        raise MetaError("la nota no puede tener caracteres de control (saltos de línea, tabs...)")
    return text


def clean_user(user) -> Optional[str]:
    user = str(user or "").strip()
    if not user:
        return None
    if len(user) > USER_MAX or _has_control(user):
        raise MetaError(f"user inválido (hasta {USER_MAX} caracteres, sin caracteres de control)")
    return user


def normalize_tags(tags) -> List[str]:
    """trim + minúsculas + sin repetidos (en orden). MetaError si no es una lista de textos."""
    if tags is None:
        return []
    if isinstance(tags, str) or not isinstance(tags, (list, tuple)):
        raise MetaError("los tags van en una lista")
    out = []
    for t in tags:
        if not isinstance(t, str):
            raise MetaError("cada tag es un texto")
        t = t.strip().lower()
        if t and t not in out:
            out.append(t)
    return out


def suggest(tag: str, ids: List[str]) -> Optional[str]:
    """El id del catálogo más parecido a `tag`, o None."""
    m = difflib.get_close_matches(tag, ids, n=1, cutoff=0.5)
    return m[0] if m else None


def check_known(tags: List[str], cat: List[dict]) -> None:
    """MetaError si algún tag no está en el catálogo (con el más parecido y la lista válida)."""
    ids = [t["id"] for t in cat]
    bad = [t for t in tags if t not in ids]
    if not bad:
        return
    hints = [f"'{t}'" + (f" (¿'{suggest(t, ids)}'?)" if suggest(t, ids) else "") for t in bad]
    raise MetaError(f"tag fuera del catálogo: {', '.join(hints)}. Válidos: {', '.join(ids)}")


def merge_tags(current: List[str], set_to: Optional[List[str]], add: List[str], remove: List[str]) -> List[str]:
    """Lista nueva: `set_to` (si viene) o la actual, + add, - remove. MetaError si pasa el tope."""
    new = list(set_to) if set_to is not None else list(current)
    new += [t for t in add if t not in new]
    new = [t for t in new if t not in remove]
    if len(new) > TAGS_PER_BOARD:
        raise MetaError(f"máximo {TAGS_PER_BOARD} tags por placa")
    return new


def warn_ids(cat: List[dict]) -> List[str]:
    return [t["id"] for t in cat if t.get("kind") == "warn"]

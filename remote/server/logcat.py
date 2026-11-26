"""
logcat.py — categoría y nivel de cada línea del log de una placa, para filtrarlo
(el monitor del dashboard y `espbench logs --cat/--level`).

Catálogo: DEFAULT_CATEGORIES (los tags del firmware de Sensify), o
meta/log_categories.json del bench, que lo reemplaza entero:

    {"categories": [{"id": "celular", "label": "Celular",
                     "tags": ["BG95", "MB_*"], "lines": ["^Unregistered"]}, ...]}

- `tags`: el tag de una línea de ESP-IDF (`I (123) BG95: ...`), exacto o prefijo con `*`
  al final. Primero los exactos de todas las categorías; después los prefijos, en orden.
- `lines`: regex (búsqueda, sin flags) sobre el cuerpo de las líneas sin tag de ESP-IDF;
  la primera categoría que matchea, en orden. Tienen que andar igual en Python y JS.
- Dos categorías fijas al final: `banco` (líneas del server, origen `|`) y `otros`.

Nivel: E, W, I, D o V. Una línea de ESP-IDF, su letra; el inicio de un panic, E; un
`rst:`, W (lo mismo que serial_watch.line_kind); una del server, la de taglog.

Una línea sin tag que no matchea ninguna regex ni es panic/reset **hereda** la
categoría y el nivel de la anterior: el backtrace de un panic, las líneas sueltas
que imprime un componente después de su línea con tag. Un rango que empieza en el
medio de una de esas rachas las ve como `otros` (no tiene la anterior).

La misma clasificación está en espbench.js (EB.logCategorizer):
tests/test_logcat_parity.py.
"""
import json
import re
from typing import Dict, List, Optional, Tuple

from server import paths
from server.serial_watch import line_kind

BUILTIN = (("banco", "Banco"), ("otros", "Otros"))
LEVELS = ("E", "W", "I")             # los del filtro; I incluye D y V

DEFAULT_CATEGORIES: List[dict] = [
    {"id": "celular", "label": "Celular",
     "tags": ["BG95", "EG912", "SIM", "APN_MAP", "RatHandler", "ModemSetup", "PS", "uartcpp", "UARTcpp"],
     "lines": [r"^(Unregistered|Registered|Searching\.\.\.|Camping on|Roaming)"]},
    {"id": "controlador", "label": "Controlador",
     "tags": ["MB_*", "PORT_MODBUS", "Controller*", "ParametersScheduler", "SfyParameters", "DATA",
              "EYE", "pEYE", "rEYE"],
     "lines": [r"^(bits_\d+|net|strength|tint|tevap|tcond|voltage|cksum):", r"^\s*applying \d+/\d+"]},
    {"id": "nube", "label": "Nube",
     "tags": ["MQTT", "mqtt_client", "esp-tls", "transport_base", "CloudLogic", "ToPlatformPending",
              "SFY-DATA", "SfyParamHTTP", "TIME", "DataConsumption"],
     "lines": [r"^MQTT enviando"]},
    {"id": "wifi-ble", "label": "WiFi/BLE",
     "tags": ["SCAN", "WDrv", "ESPNOW", "BLE", "ble*", "paramsBLE", "BTDM_INIT", "coexist", "wifi", "phy_init",
              "net80211"],
     "lines": []},
    {"id": "sistema", "label": "Sistema",
     "tags": ["boot", "boot.*", "cpu_start", "heap_init", "esp_psram", "quad_psram", "spi_flash", "efuse_init",
              "app_init", "esp_image", "main_task", "flash_encrypt", "MAIN", "main_*", "SfyConfig", "SfyLogger",
              "DeviceIdentity", "DeviceRegister", "FlashStorage", "SET_CONFIG", "sleep", "pm", "gpio", "nvs",
              "nvs_sec_provider", "task_wdt", "esp_core_dump_flash", "esp_core_dump_elf", "LOOP"],
     "lines": [r"^(rst:0x|load:0x|entry 0x|configsip|clk_drv|mode:\w+, clock|ets |\[BOOTSEQ\]"
               r"|[░▒▓█ ]{10,}|-{10,}|ho \d+ tail|FIRMWARE VERSION|CHIPID)",
               r"Guru Meditation Error|abort\(\) was called|Brownout detector|stack overflow in task|assert failed:"]},
    {"id": "ota", "label": "OTA",
     "tags": ["OTA", "esp_ota_ops", "esp_https_ota"],
     "lines": [r"^\s*(Installing\.\.\. |\.+$)"]},
    {"id": "memoria", "label": "Memoria",
     "tags": ["freeheap", "heap"],
     "lines": [r"^HBMON "]},
]

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
# Línea de ESP-IDF: "I (123) TAG: ..." (o con la hora del sistema "I (12:34:56.789) TAG ...").
# El tag es la primera palabra: "BG95 ./lib/x.cpp:12 fn(): ..." → BG95.
IDF_RE = re.compile(r"^([EWIDV]) \([^)]*\) ([^:\s]+)")
# Línea del server: "WARN  | protocol       | msg"; en un log viejo sin prefijo, con la fecha adelante.
TAGLOG_RE = re.compile(r"^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| )?(INFO|WARN|ERROR|DEBUG)\s*\| ")
_TAGLOG_LEVEL = {"INFO": "I", "WARN": "W", "ERROR": "E", "DEBUG": "D"}

Cls = Tuple[str, Optional[str]]          # (categoría, nivel)


class CatalogError(ValueError):
    pass


def validate(data) -> List[dict]:
    """Las categorías de un meta/log_categories.json, normalizadas, o CatalogError."""
    cats = data.get("categories") if isinstance(data, dict) else None
    if not isinstance(cats, list) or not cats:
        raise CatalogError('falta "categories" (una lista)')
    out, seen = [], {b for b, _ in BUILTIN}
    for i, c in enumerate(cats):
        where = f"categories[{i}]"
        if not isinstance(c, dict):
            raise CatalogError(f"{where}: tiene que ser un objeto")
        cid = c.get("id")
        if not isinstance(cid, str) or not _ID_RE.match(cid):
            raise CatalogError(f"{where}: id inválido (minúsculas, números y guiones, hasta 24)")
        if cid in seen:
            raise CatalogError(f"{where}: id {cid!r} repetido o reservado")
        seen.add(cid)
        label = c.get("label", cid)
        if not isinstance(label, str) or not 0 < len(label) <= 24:
            raise CatalogError(f"{where}: label inválido (hasta 24 caracteres)")
        tags, lines = c.get("tags", []), c.get("lines", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) and t and t != "*" for t in tags):
            raise CatalogError(f"{where}: tags tiene que ser una lista de textos")
        if not isinstance(lines, list) or not all(isinstance(p, str) and p for p in lines):
            raise CatalogError(f"{where}: lines tiene que ser una lista de regex")
        for p in lines:
            try:
                re.compile(p)
            except re.error as e:
                raise CatalogError(f"{where}: regex inválida {p!r}: {e}")
        out.append({"id": cid, "label": label, "tags": list(tags), "lines": list(lines)})
    return out


_cache: Dict[str, object] = {}


def load() -> dict:
    """{"categories": [...], "source": "default"|"bench", "error": None|texto}. El archivo del
    bench reemplaza al default; si está roto, el default y el error (el dashboard lo muestra)."""
    path = paths.log_categories_file()
    try:
        st = path.stat()
    except OSError:
        return {"categories": DEFAULT_CATEGORIES, "source": "default", "error": None}
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _cache.get("key") == key:
        return _cache["value"]                          # type: ignore[return-value]
    try:
        value = {"categories": validate(json.loads(path.read_text(encoding="utf-8"))),
                 "source": "bench", "error": None}
    except (OSError, ValueError) as e:                  # ValueError: JSON roto o CatalogError
        value = {"categories": DEFAULT_CATEGORIES, "source": "default",
                 "error": f"{path.name}: {e}"}
    _cache.update(key=key, value=value)
    return value


def public(catalog: dict) -> dict:
    """Lo que devuelve GET /api/log-categories: el catálogo con las fijas al final."""
    cats = [dict(c) for c in catalog["categories"]]
    cats += [{"id": i, "label": label, "tags": [], "lines": [], "builtin": True} for i, label in BUILTIN]
    return {"categories": cats, "levels": list(LEVELS), "source": catalog["source"], "error": catalog["error"]}


def ids(catalog: dict) -> List[str]:
    return [c["id"] for c in catalog["categories"]] + [b for b, _ in BUILTIN]


class Classifier:
    """feed(origin, body) → (categoría, nivel), en orden de líneas (la herencia mira la anterior).
    `body`: el cuerpo ya limpio (sin ANSI, con el \\r aplicado: logrange.clean_text)."""

    def __init__(self, categories: List[dict]):
        self._exact: Dict[str, str] = {}
        self._prefix: List[Tuple[str, str]] = []
        self._lines = [(re.compile(p), c["id"]) for c in categories for p in c.get("lines", [])]
        for c in categories:
            for t in c.get("tags", []):
                if t.endswith("*"):
                    self._prefix.append((t[:-1], c["id"]))
                else:
                    self._exact.setdefault(t, c["id"])
        self.prev: Optional[Cls] = None

    def tag_category(self, tag: str) -> str:
        cat = self._exact.get(tag)
        if cat is None:
            cat = next((c for p, c in self._prefix if tag.startswith(p)), "otros")
        return cat

    def classify(self, origin: Optional[str], body: str, prev: Optional[Cls]) -> Cls:
        if origin == "|" or origin is None and TAGLOG_RE.match(body):
            m = TAGLOG_RE.match(body)
            return "banco", _TAGLOG_LEVEL[m.group(1)] if m else None
        m = IDF_RE.match(body)
        if m:
            return self.tag_category(m.group(2)), m.group(1)
        kind = line_kind(body)
        level = "E" if kind == "panic" else "W" if kind == "boot" else None
        cat = next((c for rx, c in self._lines if rx.search(body)), None)
        if cat is None and level is None:
            return prev if prev is not None else ("otros", None)
        return cat or "otros", level

    def feed(self, origin: Optional[str], body: str) -> Cls:
        self.prev = self.classify(origin, body, self.prev)
        return self.prev


def parse_filter(cat: Optional[str], level: Optional[str], catalog: dict) -> Tuple[Optional[set], Optional[set]]:
    """`cat=celular,nube` y `level=E,W` del pedido → conjuntos (None = sin filtro). ValueError con
    el motivo si una categoría o un nivel no existe."""
    cats = lvls = None
    if cat:
        cats = {c.strip() for c in cat.split(",") if c.strip()}
        bad = sorted(cats - set(ids(catalog)))
        if bad:
            raise ValueError(f"categoría {', '.join(bad)} no existe en este bench (hay: {', '.join(ids(catalog))})")
    if level:
        lvls = {x.strip().upper() for x in level.split(",") if x.strip()}
        bad = sorted(lvls - set(LEVELS))
        if bad:
            raise ValueError(f"nivel {', '.join(bad)} no existe (hay: {', '.join(LEVELS)}; I incluye D y V)")
    return cats or None, lvls or None


def passes(cls: Cls, cats: Optional[set], lvls: Optional[set]) -> bool:
    cat, level = cls
    if cats is not None and cat not in cats:
        return False
    if lvls is not None:
        chip = "I" if level in ("D", "V") else level
        if chip not in lvls:
            return False
    return True

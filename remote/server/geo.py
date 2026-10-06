"""
geo.py — dónde está el bench, a nivel ciudad.

- **Automática**: geolocalización por la IP pública del bench: `GET https://ipinfo.io/json`
  (sin token) y, si falla, `https://ipapi.co/json/`. Timeout corto, un intento por servicio.
  El resultado (ciudad, región, país, lat/lon, zona horaria, `ts`) va en meta/bench_geo.json.
  La pide el api al arrancar, en un thread (no frena el arranque ni los pedidos), y cada 24 h
  (`start_background`). Si la red falla, queda lo último guardado marcado `stale`.
- **Manual (override)**: un texto fijo en meta/bench_location (`PATCH /api/bench {location}`),
  para cuando la IP sale por una VPN y da otra ciudad. Borrarlo vuelve a la automática.
- **Desactivada**: con /opt/esp/geo_disabled (o `ESPBENCH_GEO=off`, lo usan los tests) el bench no
  consulta a nadie: solo vale el override manual.

`location()` es lo que expone /api/version: {label, city, region, country, lat, lon, tz, source,
ts, stale} o None. `label` lo arma el server: "Ciudad, PAÍS" (el override: su texto).
meta/ es escribible por el api (sfypi); /opt/esp es root 755.
"""
import datetime as dt
import json
import os
import tempfile
import threading
import time
import urllib.request
from typing import Callable, Optional

from server import board_meta, paths, taglog

TAG = "geo"

LOCATION_MAX = 60
TIMEOUT_S = 3.0
REFRESH_S = 24 * 3600
RETRY_S = 3600           # después de un fallo, el próximo intento (sin reintentos agresivos)
SERVICES = ("https://ipinfo.io/json", "https://ipapi.co/json/")


def _now_iso() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def disabled() -> bool:
    return paths.geo_disabled_file().exists() or os.environ.get("ESPBENCH_GEO", "").lower() in ("off", "0", "no")


# ---------- consulta ----------

def http_get_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "espbench"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _text(v, n: int = 60) -> Optional[str]:
    if not isinstance(v, str):
        return None
    v = v.strip()[:n]
    return v if v and not board_meta.has_control(v) else None


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 4) if -180 <= f <= 180 else None


def parse(url: str, data) -> Optional[dict]:
    """Respuesta de ipinfo.io o ipapi.co → {city, region, country, lat, lon, tz}, o None si no sirve
    (error del servicio, IP privada sin ciudad)."""
    if not isinstance(data, dict) or data.get("error") or data.get("bogon"):
        return None
    if "ipapi.co" in url:
        lat, lon = _num(data.get("latitude")), _num(data.get("longitude"))
        country = data.get("country_code") or data.get("country")
    else:
        lat = lon = None
        parts = str(data.get("loc") or "").split(",")
        if len(parts) == 2:
            lat, lon = _num(parts[0]), _num(parts[1])
        country = data.get("country")
    out = {"city": _text(data.get("city")), "region": _text(data.get("region")),
           "country": (_text(country, 3) or "").upper() or None, "lat": lat, "lon": lon,
           "tz": _text(data.get("timezone"), 64)}
    return out if out["city"] or out["country"] else None


def fetch(get_json: Callable = http_get_json, timeout: float = TIMEOUT_S) -> Optional[dict]:
    """La ubicación de la IP pública: el primer servicio que contesta algo útil, o None."""
    for url in SERVICES:
        try:
            r = parse(url, get_json(url, timeout))
        except Exception as e:      # red, JSON roto, HTTP 429...: el siguiente servicio
            taglog.warn(TAG, f"{url}: {e}")
            continue
        if r:
            return dict(r, service=url.split("/")[2])
    return None


# ---------- archivos ----------

def _write_atomic(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
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


def read_auto() -> Optional[dict]:
    try:
        d = json.loads(paths.bench_geo_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def read_manual() -> Optional[str]:
    try:
        return paths.bench_location_file().read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def set_manual(text) -> None:
    """Fija el override ("" o None lo borra: vuelve la automática). board_meta.MetaError si es largo
    (más de LOCATION_MAX) o tiene caracteres de control o de formato."""
    text = board_meta.clean_text(text, "la ubicación", LOCATION_MAX) or None
    path = paths.bench_location_file()
    if text is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _write_atomic(path, text + "\n")


def refresh(get_json: Callable = http_get_json, now: Callable[[], str] = _now_iso) -> Optional[dict]:
    """Consulta y guarda. Si falla, deja lo último guardado con `stale: true`. Desactivada: nada."""
    if disabled():
        return None
    r = fetch(get_json)
    if r:
        data = dict(r, source="auto", ts=now(), stale=False)
        taglog.info(TAG, f"ubicación por IP: {label(data)} ({r['service']})")
    else:
        old = read_auto()
        if not old:
            taglog.warn(TAG, "no pude averiguar la ubicación por IP (sin red o los servicios no contestan)")
            return None
        if old.get("stale"):
            return old
        data = dict(old, stale=True)
        taglog.warn(TAG, f"no pude actualizar la ubicación por IP: queda la de {old.get('ts')}")
    try:
        _write_atomic(paths.bench_geo_file(), json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except OSError as e:
        taglog.error(TAG, f"no se pudo guardar {paths.bench_geo_file()}: {e}")
    return data


# ---------- lo que se expone ----------

def label(d: dict) -> Optional[str]:
    """"Ciudad, PAÍS"; sin ciudad, "Región, PAÍS" o el país."""
    place = d.get("city") or d.get("region")
    parts = [p for p in (place, d.get("country")) if p]
    return ", ".join(parts) or None


def location() -> Optional[dict]:
    """La ubicación del bench: el override manual si hay; si no, la automática (salvo desactivada)."""
    keys = ("city", "region", "country", "lat", "lon", "tz")
    manual = read_manual()
    if manual:
        try:
            ts = dt.datetime.fromtimestamp(paths.bench_location_file().stat().st_mtime).astimezone()
            ts = ts.replace(microsecond=0).isoformat()
        except OSError:
            ts = None
        return dict({k: None for k in keys}, label=manual, source="manual", ts=ts, stale=False)
    if disabled():
        return None
    auto = read_auto()
    if not auto or not label(auto):
        return None
    return dict({k: auto.get(k) for k in keys}, label=label(auto), source="auto", ts=auto.get("ts"),
                stale=bool(auto.get("stale")))


def _age_s(d: Optional[dict]) -> Optional[float]:
    try:
        return time.time() - dt.datetime.fromisoformat(d["ts"]).timestamp()
    except (TypeError, KeyError, ValueError):
        return None


_started = False


def start_background(get_json: Callable = http_get_json, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Thread del api: consulta al arrancar si lo guardado tiene más de 24 h (o no hay) y después cada
    24 h; tras un fallo, de nuevo en una hora. Una sola vez por proceso. False si está desactivada."""
    global _started
    if _started or disabled():
        return False
    _started = True

    def loop():
        while True:
            if disabled():
                sleep(REFRESH_S)
                continue
            age = _age_s(read_auto())
            if age is None or age >= REFRESH_S or (read_auto() or {}).get("stale"):
                d = refresh(get_json)
                wait = REFRESH_S if d and not d.get("stale") else RETRY_S
            else:
                wait = REFRESH_S - age
            sleep(max(60.0, wait))

    threading.Thread(target=loop, name="geo", daemon=True).start()
    return True

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paths.py — Fuente única de las rutas generadas en runtime bajo ESP_BASE.

Ningún otro módulo debe hardcodear "/opt/esp" ni reconstruir a mano
"<logs>/<tty>/mac" o similares. Todo lo que se lee o escribe en el
filesystem del server pasa por acá.

ESP_BASE se lee de la variable de entorno del mismo nombre (default
"/opt/esp"). Se relee en cada llamada — no se cachea a nivel de módulo —
para que los tests puedan pisarla con monkeypatch.setenv() sin pelear
con el orden de imports.

Dos esquemas de rutas por dispositivo, más el estado runtime (run/<tty>.json):
- por device, keyed por MAC (device_home, device_output_log, device_jobs_dir,
  ...): donde va todo lo de un device identificado. Sigue a la placa aunque
  cambie de puerto.
- por tty (lock_file, current_elf_file, last_user_file, ...): el lock (a
  propósito, ver docs/ARCHITECTURE.md §5), los devices sin MAC, y lo que
  dejaron sesiones del esquema anterior.
"""
import os
import pathlib

_DEFAULT_BASE = "/opt/esp"


def esp_base() -> pathlib.Path:
    return pathlib.Path(os.environ.get("ESP_BASE", _DEFAULT_BASE))


# ---------- directorios de primer nivel ----------

def logs_dir() -> pathlib.Path:
    return esp_base() / "logs"


def jobs_dir() -> pathlib.Path:
    return esp_base() / "jobs"


def locks_dir() -> pathlib.Path:
    return esp_base() / "locks"


def devices_dir() -> pathlib.Path:
    return esp_base() / "devices"


def devices_file() -> pathlib.Path:
    return esp_base() / "devices.json"


def version_file() -> pathlib.Path:
    return esp_base() / "VERSION"


def api_token_file() -> pathlib.Path:
    """Token opcional de la API y del flash (auth.py)."""
    return esp_base() / "api_token"


def update_conf_file() -> pathlib.Path:
    """REPO_DIR y PIN de espbench-update (lo escribe install.sh)."""
    return esp_base() / "update.conf"


def update_status_file() -> pathlib.Path:
    """Resultado del último espbench-update (no va en run/: ahí todo es un tty)."""
    return esp_base() / "update_status.json"


def bench_name_file() -> pathlib.Path:
    """(opcional) Nombre del bench para bench-master. Sin él, el hostname."""
    return esp_base() / "bench_name"


def run_dir() -> pathlib.Path:
    return esp_base() / "run"


def slots_file() -> pathlib.Path:
    return esp_base() / "slots.conf"


# ---------- rutas por tty (esquema vigente) ----------

def tty_log_dir(tty_name: str) -> pathlib.Path:
    return logs_dir() / tty_name


def mac_file(tty_name: str) -> pathlib.Path:
    return tty_log_dir(tty_name) / "mac"


def last_user_file(tty_name: str) -> pathlib.Path:
    return tty_log_dir(tty_name) / "last_user"


def lock_file(tty_name: str) -> pathlib.Path:
    return locks_dir() / tty_name


def current_elf_file(tty_name: str) -> pathlib.Path:
    return esp_base() / f"current_{tty_name}.elf"


def tty_state_file(tty_name: str) -> pathlib.Path:
    """Estado runtime del proceso que atiende ese tty (ver runstate.py)."""
    return run_dir() / f"{tty_name}.json"


# ---------- rutas por device, keyed por MAC (esquema nuevo) ----------

def _normalize_mac(mac: str) -> str:
    return mac.upper().replace(":", "").replace("-", "")


def device_home(mac: str) -> pathlib.Path:
    return devices_dir() / _normalize_mac(mac)


def device_output_log(mac: str) -> pathlib.Path:
    return device_home(mac) / "output.log"


def events_file_beside(log_path) -> pathlib.Path:
    """events.jsonl vive en el mismo directorio que el output.log de la placa
    (devices/<mac>/ o devices/unknown-<tty>/). El proceso del api llega acá
    desde el log_path de run/<tty>.json."""
    return pathlib.Path(log_path).parent / "events.jsonl"


def device_events_file(mac: str) -> pathlib.Path:
    """Registro de eventos de la placa (boot, panic, flash, state...)."""
    return events_file_beside(device_output_log(mac))


def device_current_elf(mac: str) -> pathlib.Path:
    return device_home(mac) / "current.elf"


def device_jobs_dir(mac: str) -> pathlib.Path:
    return device_home(mac) / "jobs"


def device_last_user(mac: str) -> pathlib.Path:
    return device_home(mac) / "last_user"


def device_unknown_home(tty_name: str) -> pathlib.Path:
    """Hogar provisorio de un device cuya MAC nunca se pudo leer."""
    return devices_dir() / f"unknown-{tty_name}"


def unknown_output_log(tty_name: str) -> pathlib.Path:
    return device_unknown_home(tty_name) / "output.log"


def unknown_events_file(tty_name: str) -> pathlib.Path:
    return events_file_beside(unknown_output_log(tty_name))

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

Incluye dos esquemas de rutas por dispositivo:
- por tty (mac_file, last_user_file, lock_file, current_elf_file):
  esquema vigente hoy, keyed por nombre de puerto (ttyUSBN).
- por device (device_home, device_output_log, ...): esquema nuevo,
  keyed por MAC — sobrevive a que el device cambie de tty al desconectar
  y reconectar. Todavía no lo escribe nadie (ver plan de refactor).
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


# ---------- rutas por device, keyed por MAC (esquema nuevo) ----------

def _normalize_mac(mac: str) -> str:
    return mac.upper().replace(":", "").replace("-", "")


def device_home(mac: str) -> pathlib.Path:
    return devices_dir() / _normalize_mac(mac)


def device_output_log(mac: str) -> pathlib.Path:
    return device_home(mac) / "output.log"


def device_current_elf(mac: str) -> pathlib.Path:
    return device_home(mac) / "current.elf"


def device_jobs_dir(mac: str) -> pathlib.Path:
    return device_home(mac) / "jobs"


def device_state_file(mac: str) -> pathlib.Path:
    return device_home(mac) / "state.json"

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flash.py — Funciones de flasheo con esptool para ESP32
"""

import json, logging, pathlib, re, shlex, shutil, subprocess, sys, threading, time
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from server import taglog

TAG = "flash"

DEFAULT_FLASH_FILES = [
    ("0x1000", "bootloader.bin"),
    ("0x8000", "partition-table.bin"),
    ("0xe000", "ota_data_initial.bin"),
    ("0x10000", "app.bin")
]


def find_esptool_cmd():
    # Buscar esptool.py en el PATH
    exe = shutil.which("esptool.py")
    if exe:
        return [exe]

    # Buscar esptool como módulo Python
    try:
        import esptool
        return [sys.executable, "-m", "esptool"]
    except ImportError:
        pass

    raise RuntimeError("esptool no encontrado. Instala con: pip install esptool")


def build_esptool_cmd(esptool_cmd, chip, port, baud, encrypt, erase, jobdir: pathlib.Path):
    fa = jobdir / "flasher_args.json"
    taglog.debug(TAG, f"leyendo {fa}")
    J = json.loads(fa.read_text(encoding="utf-8"))
    taglog.debug(TAG, f"contenido: {json.dumps(J, indent=2)}")

    pairs = []
    ff = J.get("flash_files")

    # Intentar como diccionario primero
    if isinstance(ff, dict):
        taglog.debug(TAG, f"flash_files es diccionario con {len(ff)} entradas")
        for off, path in ff.items():
            if off and path:
                p = pathlib.Path(path)
                # Buscar archivo con diferentes estrategias
                candidates = [
                    p if p.is_absolute() else jobdir / p,
                    jobdir / p.name,
                    jobdir / p.parts[-1] if p.parts else jobdir / str(p)
                ]
                found = False
                for cand in candidates:
                    if cand.exists():
                        taglog.debug(TAG, f"encontrado: {off} → {cand}")
                        pairs.append((off, str(cand)))
                        found = True
                        break
                if not found:
                    taglog.warn(TAG, f"no encontrado: {off} → {path}")

    # Intentar como lista
    elif isinstance(ff, list):
        taglog.debug(TAG, f"encontrados {len(ff)} entradas en flash_files")
        for it in ff:
            off, path = None, None
            if isinstance(it, (list, tuple)) and len(it) >= 2:
                off, path = it[0], it[1]
            elif isinstance(it, dict):
                off = it.get("offset")
                path = it.get("file") or it.get("bin_file") or it.get("path")
            if off and path:
                p = pathlib.Path(path)
                cand = p if p.is_absolute() else (jobdir / p.name if (jobdir / p.name).exists() else jobdir / path)
                if cand.exists():
                    taglog.debug(TAG, f"agregando par: {off} → {cand}")
                    pairs.append((off, str(cand)))
                else:
                    taglog.warn(TAG, f"no encontrado: {off} → {cand}")

    # Si no se encontraron pares, buscar archivos por defecto
    if not pairs:
        taglog.debug(TAG, f"no se encontraron archivos en flash_files, buscando archivos por defecto...")
        for off, name in DEFAULT_FLASH_FILES:
            p = jobdir / name
            if p.exists():
                taglog.debug(TAG, f"encontrado: {off} → {p}")
                pairs.append((off, str(p)))
            else:
                taglog.debug(TAG, f"no encontrado: {p}")

    # Si aún no hay pares, buscar cualquier .bin en el directorio
    if not pairs:
        taglog.debug(TAG, f"buscando cualquier archivo .bin en {jobdir}...")
        for bin_file in jobdir.glob("*.bin"):
            taglog.debug(TAG, f"archivo .bin encontrado: {bin_file}")
            # Intentar asignar offsets comunes basados en el nombre
            name = bin_file.name.lower()
            if "bootloader" in name:
                pairs.append(("0x1000", str(bin_file)))
                taglog.debug(TAG, f"asignado bootloader: 0x1000 → {bin_file}")
            elif "partition" in name:
                pairs.append(("0x8000", str(bin_file)))
                taglog.debug(TAG, f"asignado partition: 0x8000 → {bin_file}")
            elif "ota" in name:
                pairs.append(("0xe000", str(bin_file)))
                taglog.debug(TAG, f"asignado ota: 0xe000 → {bin_file}")
            elif "app" in name or "main" in name or "firmware" in name or bin_file.name.endswith(".bin"):
                # Asignar a offset común de aplicación
                pairs.append(("0x10000", str(bin_file)))
                taglog.debug(TAG, f"asignado app: 0x10000 → {bin_file}")

    taglog.info(TAG, f"total de pares encontrados: {len(pairs)}")
    if len(pairs) == 0:
        raise FileNotFoundError("No se encontraron archivos .bin para flashear")

    # Verificar si tenemos archivos críticos
    offsets = [off for off, _ in pairs]
    critical_files = []

    # Verificar archivos críticos basados en offsets comunes
    if "0x1000" not in offsets:
        critical_files.append("bootloader (0x1000)")
    if "0x10000" not in offsets and "0x120000" not in offsets:
        critical_files.append("aplicación (0x10000 o 0x120000)")

    if critical_files:
        taglog.warn(TAG, f"Faltan archivos críticos: {', '.join(critical_files)}")
        taglog.warn(TAG, "El ESP32 puede no funcionar correctamente sin estos archivos")

        # Si falta la aplicación, es crítico
        if "aplicación" in str(critical_files):
            taglog.error(TAG, "Falta el archivo de aplicación principal")
            taglog.error(TAG, "Sin aplicación, el ESP32 no funcionará correctamente")
            taglog.warn(TAG, "Continuando con flasheo parcial...")
            taglog.warn(TAG, "El ESP32 necesitará ser flasheado con aplicación después")

    taglog.info(TAG, f"Archivos listos para flashear: {len(pairs)}")
    for off, path in pairs:
        taglog.info(TAG, f"  ✓ {off}: {pathlib.Path(path).name}")

    erase_cmd = None
    if erase:
        erase_cmd = esptool_cmd + ["--chip", chip, "--port", port, "--baud", str(baud), "erase-flash"]

    write_cmd = esptool_cmd + ["--chip", chip, "--port", port, "--baud", str(baud),
                               "--before", "default-reset", "--after", "hard-reset",
                               "write-flash", "-z"]
    if encrypt:
        write_cmd.insert(write_cmd.index("write-flash") + 1, "--encrypt")
    for off, p in pairs:
        write_cmd += [off, p]
    return erase_cmd, write_cmd, pairs


_MAC_RE = re.compile(r"MAC:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
_MAC_SERIAL_RE = re.compile(r'mac\s*=\s*([0-9A-Fa-f]{12})', re.IGNORECASE)
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
_MAC_POLL = 0.2      # cada cuánto read_mac mira `stop` mientras esptool lee


def read_mac(port: str, stop: Optional[threading.Event] = None, timeout: float = 15.0) -> Optional[str]:
    """Run esptool read_mac on port. Returns MAC string (uppercase, colons) or None.

    Con `stop`: si se setea mientras esptool lee (una señal de terminación al
    arrancar), esptool se termina enseguida y devuelve None."""
    try:
        esptool = find_esptool_cmd()
        proc = subprocess.Popen(esptool + ["--port", port, "read_mac"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except Exception:
        return None
    out = ""
    try:
        deadline = time.monotonic() + timeout
        while True:
            if stop is not None and stop.is_set():
                _terminate(proc)
                return None
            try:
                out, _ = proc.communicate(timeout=_MAC_POLL)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    _terminate(proc)
                    return None
    except Exception:
        _terminate(proc)
        return None
    for line in (out or "").splitlines():
        m = _MAC_RE.search(line)
        if m:
            return m.group(1).upper()
    return None


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM y, si no alcanza, SIGKILL: que esptool no quede con el puerto abierto."""
    try:
        proc.terminate()
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=2)
    except Exception:
        pass


def parse_mac_from_serial(text: str) -> Optional[str]:
    """Parse MAC from firmware serial output (e.g. 'mac = F8B3B7D848A8'). Returns XX:XX:XX:XX:XX:XX or None."""
    clean = _ANSI_RE.sub('', text)
    m = _MAC_SERIAL_RE.search(clean)
    if not m:
        return None
    raw = m.group(1).upper()
    return ':'.join(raw[i:i+2] for i in range(0, 12, 2))


def run_cmd(cmd, log: Optional[logging.Logger] = None, on_line=None):
    """Corre esptool línea a línea. Cada línea va a taglog (TAG "esptool": stdout
    + log del device), al log del job si hay, y a on_line (stream al cliente)."""
    shown = " ".join(shlex.quote(c) for c in cmd)
    if log:
        log.info("RUN: %s", shown)
    taglog.info(TAG, f">>> {shown}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        stripped = line.rstrip()
        if log:
            log.info(stripped)
        taglog.info("esptool", stripped)
        if on_line:
            on_line(stripped)
    rc = proc.wait()
    if log:
        log.info("EXIT %d", rc)
    taglog.info(TAG, f"<<< exit code {rc}")
    return rc

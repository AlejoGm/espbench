#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
erase.py — modo Erase Region interactivo (Ctrl-E en la sesión tmux del device).

Antes vivía en monitor.py y entraba directo a los privados de EspMonitor
(_stdin_access_lock, _restore_stdin, _set_stdin_raw). Ahora solo usa su
interfaz pública: get_recent_output(), interactive_input(), stop(), start().

El borrado pasa por la FSM del device (start_erase/finish_erase): no se puede
borrar mientras se flashea, y mientras se borra el proceso no se deja matar
por una señal (Device.busy).
"""
import sys
from typing import List, Optional

from server import taglog
from server.flash import find_esptool_cmd, run_cmd
from server.device import InvalidTransition
from server.partition_table import parse_partition_table

TAG = "erase"


def _say(msg: str = "") -> None:
    sys.stdout.write(msg + "\r\n")
    sys.stdout.flush()


def select_partitions(partitions: List[dict], selection: str) -> Optional[List[dict]]:
    """'all' → todas; '1,3' → esas (1-based); '' → None (pasar a manual).
    Índices fuera de rango se ignoran con aviso; entrada inválida → None."""
    selection = selection.strip()
    if not selection:
        return None
    if selection.lower() == "all":
        return list(partitions)
    try:
        indices = [int(x.strip()) for x in selection.split(",")]
    except ValueError:
        _say("[erase] Entrada inválida, usando modo manual...")
        return None
    chosen = []
    for idx in indices:
        if 1 <= idx <= len(partitions):
            chosen.append(partitions[idx - 1])
        else:
            _say(f"[erase] Índice inválido: {idx}")
    return chosen or None


def parse_manual_region(text: str) -> Optional[List[dict]]:
    """'0x9000 0x6000' → [{offset, size, name='manual'}]; inválido → None."""
    parts = text.split()
    if len(parts) < 2:
        return None
    try:
        return [{"offset": int(parts[0], 16), "size": int(parts[1], 16), "name": "manual"}]
    except ValueError:
        return None


def erase_region_interactive(mon, cfg: dict, device) -> None:
    _say()
    _say("=" * 60)
    _say("[erase] Modo Erase Region activado")
    _say("=" * 60)

    partitions = parse_partition_table(mon.get_recent_output())
    regions = None
    if partitions:
        _say(f"\r\n[erase] Tabla de particiones detectada ({len(partitions)} particiones):\r\n")
        for i, p in enumerate(partitions, 1):
            _say(f"  {i}. {p['name']:20s} @ {p['offset_hex']:>10s} ({p['size_hex']:>10s} bytes)")
        regions = select_partitions(partitions, mon.interactive_input(
            "\r\n[erase] Particiones a borrar (ej: 1,3,5 o 'all'), Enter para manual: "))
    else:
        _say("\r\n[erase] No se detectó tabla de particiones")

    if not regions:
        answer = mon.interactive_input(
            "\r\n[erase] Offset y tamaño en hex (ej: 0x9000 0x6000), o 'cancel': ")
        if answer.lower() == "cancel":
            _say("[erase] Operación cancelada")
            return
        regions = parse_manual_region(answer)
        if not regions:
            _say("[erase] Formato inválido. Usa: offset_hex size_hex")
            return

    _say(f"\r\n[erase] Se borrarán {len(regions)} región(es):")
    for r in regions:
        _say(f"  - {r.get('name', 'manual')}: offset 0x{r['offset']:x}, tamaño 0x{r['size']:x}")
    if mon.interactive_input("\r\n[erase] ¿Confirmar? (s/N): ").lower() != "s":
        _say("[erase] Operación cancelada")
        return

    try:
        device.start_erase()
    except InvalidTransition:
        _say(f"[erase] No se puede borrar ahora: el device está en {device.state.value}")
        return

    try:
        esptool = find_esptool_cmd()
        tty = cfg.get("tty")
        try:
            _say("\r\n[erase] Deteniendo monitor temporalmente...")
            mon.stop()
            for r in regions:
                name = r.get("name", "manual")
                offset_hex, size_hex = f"0x{r['offset']:x}", f"0x{r['size']:x}"
                taglog.info(TAG, f"borrando {name} @ {offset_hex} (tamaño {size_hex})")
                cmd = esptool + ["--port", tty, "--after", "no-reset",
                                 "erase_region", offset_hex, size_hex, "--force"]
                rc = run_cmd(cmd)
                if rc == 0:
                    taglog.info(TAG, f"región {name} borrada")
                else:
                    taglog.error(TAG, f"región {name}: esptool terminó con código {rc}")
        finally:
            _say("\r\n[erase] Reiniciando monitor...")
            mon.start()
        taglog.info(TAG, "operación completada")
    except Exception as e:
        taglog.error(TAG, f"erase_region falló: {e}")
    finally:
        device.finish_erase()

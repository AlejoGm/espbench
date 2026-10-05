#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
partition_table.py — parseo de la tabla de particiones que imprime el
bootloader del ESP32 al arrancar.

Lógica pura (texto → lista de dicts), sin PTY ni esptool: vivía enterrada en
monitor.py y por eso no tenía tests. La usa erase.py para ofrecer las
particiones en el modo Erase Region (Ctrl-E).
"""


def parse_partition_table(text: str) -> list[dict]:
    """
    Parsea una tabla de particiones del ESP32 desde texto del bootloader.
    Formato esperado:
    I (71) boot:  0 nvs              WiFi data        01 02 00012000 00100000

    Retorna lista de diccionarios con: name, type, subtype, offset, size
    """
    import re
    partitions = []

    # Patrón para el formato del bootloader del ESP32:
    # I (XX) boot:  N nombre          descripción     TT SS OOOOOOOO LLLLLLLL
    # Ejemplo: I (71) boot:  0 nvs              WiFi data        01 02 00012000 00100000
    # El patrón busca: I (número) boot: número nombre [descripción con espacios] tipo subtipo offset length
    pattern = r'I\s*\(\s*\d+\s*\)\s+boot:\s+\d+\s+(\w+)\s+[^\d]+\s+([0-9a-fA-F]{2})\s+([0-9a-fA-F]{2})\s+([0-9a-fA-F]{8})\s+([0-9a-fA-F]{8})'

    # También intentar formato CSV alternativo (por si acaso)
    pattern_csv = r'(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*([^,]*)\s*,\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)'

    in_partition_table = False

    for line in text.split('\n'):
        line_stripped = line.strip()

        # Detectar inicio de tabla de particiones
        if 'partition table' in line.lower() or '## Label' in line:
            in_partition_table = True
            continue

        # Detectar fin de tabla
        if 'end of partition table' in line.lower():
            in_partition_table = False
            continue

        if not in_partition_table and not line_stripped:
            continue

        # Intentar formato bootloader primero (más común)
        match = re.search(pattern, line)
        if match:
            name, ptype, subtype, offset_str, size_str = match.groups()
            try:
                offset = int(offset_str, 16)
                size = int(size_str, 16)
                partitions.append({
                    'name': name.strip(),
                    'type': ptype.strip(),
                    'subtype': subtype.strip(),
                    'offset': offset,
                    'offset_hex': f"0x{offset_str}",
                    'size': size,
                    'size_hex': f"0x{size_str}"
                })
                continue
            except ValueError:
                pass

        # Intentar formato CSV como fallback
        if not match:
            match_csv = re.search(pattern_csv, line)
            if match_csv:
                name, ptype, subtype, flags, offset_str, size_str = match_csv.groups()
                try:
                    # Remover 0x si está presente
                    offset_clean = offset_str.replace('0x', '').replace('0X', '')
                    size_clean = size_str.replace('0x', '').replace('0X', '')
                    offset = int(offset_clean, 16)
                    size = int(size_clean, 16)
                    partitions.append({
                        'name': name.strip(),
                        'type': ptype.strip(),
                        'subtype': subtype.strip(),
                        'offset': offset,
                        'offset_hex': offset_str if offset_str.startswith('0x') else f"0x{offset_clean}",
                        'size': size,
                        'size_hex': size_str if size_str.startswith('0x') else f"0x{size_clean}"
                    })
                except ValueError:
                    continue

    return partitions

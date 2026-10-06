#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auth.py — token opcional de la Pi: /opt/esp/api_token (paths.api_token_file()).

Si el archivo existe y no está vacío:
- api.py exige `Authorization: Bearer <token>` en las escrituras (401 si no);
  las lecturas siguen abiertas.
- remote_esp32.py lo usa como token del flash cuando no viene --token: los
  .flashcfg.json sin `token` dejan de poder flashear apenas se crea el archivo.

Se lee en cada pedido: crearlo o cambiarlo no requiere reiniciar nada.
"""
import hmac
from typing import Optional

from server import paths


def read_token() -> str:
    try:
        return paths.api_token_file().read_text().strip()
    except OSError:
        return ""


def bearer_ok(authorization: Optional[str], token: Optional[str] = None) -> bool:
    """True si no hay token configurado o si el header trae el correcto."""
    token = read_token() if token is None else token
    if not token:
        return True
    if not isinstance(authorization, str):
        return False
    scheme, _, value = authorization.strip().partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value.strip(), token)

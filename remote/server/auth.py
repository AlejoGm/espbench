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


class AuthConfigError(Exception):
    """El archivo del token existe pero no se puede leer (permisos, es un
    directorio, no es texto). Falla cerrado: sin token legible no se escribe."""


def read_token() -> str:
    """El token, o "" si el archivo no existe (o está vacío) = sin auth.
    Cualquier otro error → AuthConfigError (nunca "sin token")."""
    path = paths.api_token_file()
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as e:
        raise AuthConfigError(f"no se pudo leer {path}: {e}")


def same_secret(given, expected: str) -> bool:
    """Comparación en tiempo constante, en bytes (con str no-ASCII,
    hmac.compare_digest levanta TypeError)."""
    if not isinstance(given, str):
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def bearer_ok(authorization: Optional[str], token: Optional[str] = None) -> bool:
    """True si no hay token configurado o si el header trae el correcto.
    Levanta AuthConfigError si el archivo no se puede leer."""
    token = read_token() if token is None else token
    if not token:
        return True
    if not isinstance(authorization, str):
        return False
    scheme, _, value = authorization.strip().partition(" ")
    return scheme.lower() == "bearer" and same_secret(value.strip(), token)

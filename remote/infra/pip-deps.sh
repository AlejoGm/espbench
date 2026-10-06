#!/bin/bash
# pip-deps.sh <pip> <requirements.txt> — dependencias Python del server. Lo llama
# install.sh (con set -e): una dependencia que falla aborta el install, salvo las
# opcionales, que van aparte y solo avisan.
#
# Opcional: `regex` (timeout de grep/until=re: en /api/board/{key}/log). Sin él
# logrange sigue andando con `re` y rechaza los cuantificadores anidados. Puede
# fallar en una Pi sin wheel para su Python ni compilador; antes eso cortaba el
# update entero a mitad de camino.
set -euo pipefail

PIP="$1"
REQ="$2"
OPTIONAL_RE='^[[:space:]]*regex([[:space:]]|[<>=!~;#]|$)'

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
grep -vE "$OPTIONAL_RE" "$REQ" > "$tmp" || true
"$PIP" install --quiet -r "$tmp"

while IFS= read -r spec; do
  spec="${spec%%#*}"
  spec="$(echo "$spec" | xargs)"
  [ -n "$spec" ] || continue
  if ! "$PIP" install --quiet "$spec"; then
    echo "[WARN]  no se pudo instalar '$spec' (opcional): grep/until=re: de /api/board/{key}/log" \
         "quedan sin timeout y se rechazan los cuantificadores anidados" >&2
  fi
done < <(grep -E "$OPTIONAL_RE" "$REQ" || true)

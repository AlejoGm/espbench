#!/usr/bin/env bash
# Regenerates CLAUDE.md for dirs touched in the last commit, plus README.md.
# Called by Claude Code PostToolUse hook after git commits.
# Uses claude -p (non-interactive) — requires claude CLI in PATH.

set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$REPO_ROOT"

STAMP_FILE="/tmp/espbench_docs_last_commit"
CURRENT_HASH="$(git rev-parse HEAD 2>/dev/null)" || exit 0

# Exit if already processed this commit
[ -f "$STAMP_FILE" ] && [ "$(cat "$STAMP_FILE")" = "$CURRENT_HASH" ] && exit 0

# Exit if last commit only touched docs (avoid infinite loop)
CHANGED=$(git diff HEAD~1 --name-only 2>/dev/null || exit 0)
SOURCE_CHANGED=$(echo "$CHANGED" | grep -v '^README\.md$' | grep -v 'CLAUDE\.md$' | grep -v '^scripts/' || true)

if [ -z "$SOURCE_CHANGED" ]; then
  echo "$CURRENT_HASH" > "$STAMP_FILE"
  exit 0
fi

echo "$CURRENT_HASH" > "$STAMP_FILE"

# Dirs with source changes that have a CLAUDE.md
CHANGED_DIRS=$(echo "$SOURCE_CHANGED" | xargs -I{} dirname {} | sort -u)

echo "[espbench] Actualizando docs post-commit (${CURRENT_HASH:0:7})..."

for dir in $CHANGED_DIRS; do
  CLAUDE_FILE="$dir/CLAUDE.md"
  [ -f "$CLAUDE_FILE" ] || continue
  [ "$dir" = "." ] && continue  # root handled separately

  echo "  → $CLAUDE_FILE"
  claude -p "Estás en el repo espbench ubicado en $REPO_ROOT.
Leé los archivos actuales en el directorio '$dir/' y actualizá '$CLAUDE_FILE' para que refleje con precisión el estado actual del código.
Mantené exactamente el mismo formato y estructura del archivo existente.
Solo modificá secciones que están desactualizadas respecto al código. Si todo está correcto, dejalo igual." \
    2>/dev/null || echo "  ✗ falló update de $CLAUDE_FILE (claude no disponible?)"
done

# Update README if structure-level files changed (root dir or new dirs)
ROOT_CHANGED=$(echo "$SOURCE_CHANGED" | grep -E '^[^/]+$' || true)
if [ -n "$ROOT_CHANGED" ]; then
  echo "  → README.md"
  claude -p "Estás en el repo espbench ubicado en $REPO_ROOT.
Leé la estructura actual del repo y actualizá README.md para que refleje el estado actual.
Mantené el mismo formato, tono y secciones. Solo actualizá lo que cambió (versión, estructura de archivos, features)." \
    2>/dev/null || echo "  ✗ falló update de README.md"
fi

echo "[espbench] Docs actualizados. Revisá cambios con: git diff"

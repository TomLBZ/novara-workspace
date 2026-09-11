#!/bin/sh
# init_repo.sh - instantiate the agent-handover standard in a target directory.
#
#   scripts/init_repo.sh <target-dir> [project-name] [--force]
#
# Copies templates/** into <target-dir>, replacing @PROJECT@ and @DATE@. Refuses to touch a
# non-empty directory unless --force is given. Afterwards: cd <target-dir>, run
# `tools/verify.sh docs` (expect PASS), commit, and fill the remaining @PLACEHOLDER@ markers.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SRC="$HERE/../templates"

TARGET=${1:-}
NAME=${2:-}
FORCE=0
for arg in "$@"; do [ "$arg" = "--force" ] && FORCE=1; done
[ -n "$TARGET" ] || { echo "usage: init_repo.sh <target-dir> [project-name] [--force]" >&2; exit 2; }
[ -d "$SRC" ] || { echo "init_repo.sh: templates not found at $SRC" >&2; exit 2; }

mkdir -p "$TARGET"
if [ "$FORCE" != 1 ] && [ -n "$(ls -A "$TARGET" 2>/dev/null)" ]; then
  echo "init_repo.sh: $TARGET is not empty (pass --force to write into it anyway)" >&2
  exit 2
fi
[ -n "$NAME" ] || NAME=$(basename "$(CDPATH= cd -- "$TARGET" && pwd)")
TODAY=$(date -u +%Y-%m-%d)

( cd "$SRC" && find . -type d -print ) | while read -r dir; do
  mkdir -p "$TARGET/$dir"
done
( cd "$SRC" && find . -type f -print ) | while read -r file; do
  sed -e "s|@PROJECT@|$NAME|g" -e "s|@DATE@|$TODAY|g" "$SRC/$file" > "$TARGET/$file"
done
cp "$HERE/check_docs.py" "$TARGET/tools/check_docs.py"
chmod +x "$TARGET/tools/verify.sh" "$TARGET/tools/check_docs.py" 2>/dev/null || true
echo "instantiated '$NAME' in $TARGET"
echo "next: cd $TARGET && tools/verify.sh docs   # expect RESULT: PASS"
echo "then: fill the @PLACEHOLDER@ markers in AGENTS.md, README.md, docs/design/00-overview.md"

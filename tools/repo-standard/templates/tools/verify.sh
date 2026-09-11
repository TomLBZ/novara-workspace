#!/bin/sh
# Verification entry point.
#   tools/verify.sh docs       documentation gate (AC-CORE-001) - applicable at every phase
#   tools/verify.sh g0|g1|g2   phase gates: exit 2 until the phase is reached, 1 when it fails
set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY=${PYTHON:-python3}
case "${1:-docs}" in
  docs) shift; exec "$PY" "$HERE/check_docs.py" "$@" ;;
  g0|g1|g2)
    echo "phase gate $1 is not implemented yet: write its ACs first, then implement the gate." >&2
    exit 2 ;;
  *) echo "usage: tools/verify.sh docs|g0|g1|g2" >&2; exit 2 ;;
esac

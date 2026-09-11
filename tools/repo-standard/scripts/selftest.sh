#!/bin/sh
# selftest.sh - prove the standard instantiates green and that the gate fails when it should.
#
#   scripts/selftest.sh
#
# Builds throwaway repos in $TMPDIR from templates/, runs tools/verify.sh docs against each of them,
# and asserts both the green case and six deliberate violations (plus the phase-gate exit code 2).
# Exit 0 = every expectation held.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WORK=${TMPDIR:-/tmp}/docgate-selftest.$$
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT INT TERM

TOTAL=0
FAILED=0

fresh() {
  rm -rf "$WORK/repo"
  "$HERE/init_repo.sh" "$WORK/repo" selftest >/dev/null
}

# expect <label> <expected-exit> <command...>
expect() {
  label=$1; want=$2; shift 2
  TOTAL=$((TOTAL + 1))
  if "$@" >"$WORK/out" 2>&1; then got=0; else got=$?; fi
  if [ "$got" = "$want" ]; then
    printf 'PASS  %-38s exit %s\n' "$label" "$got"
  else
    FAILED=$((FAILED + 1))
    printf 'FAIL  %-38s exit %s (wanted %s)\n' "$label" "$got" "$want"
    sed 's/^/        /' "$WORK/out"
  fi
}

echo "== documentation-gate selftest ($WORK) =="

fresh
expect "clean checkout is green" 0 "$WORK/repo/tools/verify.sh" docs

fresh
printf '\nDetails: FR-CORE-999 is not defined anywhere.\n' >>"$WORK/repo/docs/work/functional-requirements.md"
expect "unresolved ID reference" 1 "$WORK/repo/tools/verify.sh" docs

fresh
i=0; while [ $i -lt 40 ]; do printf 'padding line to blow the handover budget\n'; i=$((i + 1)); done \
  >>"$WORK/repo/docs/work/handover.md"
expect "budget overflow" 1 "$WORK/repo/tools/verify.sh" docs

fresh
printf '\n- [ ] TODO-ID: finish this later\n' >>"$WORK/repo/docs/work/roadmap.md"
expect "placeholder left behind" 1 "$WORK/repo/tools/verify.sh" docs

fresh
sed -i 's/| AC-CORE-001 |/| |/' "$WORK/repo/docs/work/functional-requirements.md"
expect "FR without an AC" 1 "$WORK/repo/tools/verify.sh" docs

fresh
printf '| AC-CORE-002 | Orphan criterion nobody requires | `true` | exit 0 |\n' \
  >>"$WORK/repo/docs/work/acceptance-criteria.md"
expect "orphan AC" 1 "$WORK/repo/tools/verify.sh" docs

fresh
rm -f "$WORK/repo/docs/design/adr/0001-"*.md
expect "missing ADR definition" 2 "$WORK/repo/tools/verify.sh" docs

fresh
expect "unreached phase gate exits 2" 2 "$WORK/repo/tools/verify.sh" g0

echo "== $((TOTAL - FAILED))/$TOTAL expectations held =="
[ "$FAILED" = 0 ]

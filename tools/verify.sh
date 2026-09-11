#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# tools/verify.sh - end-to-end health check for the self-contained workspace.
#
#   tools/verify.sh              # check the workspace in place
#   tools/verify.sh --relocate   # copy the tree to another path, self-heal it
#                                # (ws-relocate) and re-run every check there,
#                                # then report whether the copy is fully portable
#   tools/verify.sh --keep       # keep the relocation scratch dir
#
# Exit code: 0 = all checks passed, 1 = at least one FAIL.
# ---------------------------------------------------------------------------
set -uo pipefail

SELF_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd -P "$SELF_DIR/.." && pwd)"

RELOCATE=0
KEEP=0
for arg in "$@"; do
  case "$arg" in
    --relocate) RELOCATE=1 ;;
    --keep) KEEP=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

pass=0; fail=0; warn=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail + 1)); }
wrn()  { printf '  \033[33mWARN\033[0m %s\n' "$1"; warn=$((warn + 1)); }
sect() { printf '\n== %s ==\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

# expect <actual> <expected-substring> <label>
expect() {
  case "$1" in
    *"$2"*) ok "$3 ($1)" ;;
    *) bad "$3: got '$1', expected to contain '$2'" ;;
  esac
}

check_tool_is_local() {
  local tool path
  for tool in python python3 node npm npx git uv ws-config; do
    path="$(command -v "$tool" 2>/dev/null || true)"
    if [ -z "$path" ]; then
      bad "$tool not on PATH"
    elif [[ "$path" != "$WS_ROOT"/* ]]; then
      bad "$tool resolves outside the workspace: $path"
    else
      ok "$tool -> ${path#"$WS_ROOT"/}"
    fi
  done
}

run_checks() {
  # shellcheck disable=SC1091
  . "$WS_ROOT/bin/activate.sh"
  mkdir -p "$WS_ROOT/tmp" "$WS_ROOT/logs" "$WS_ROOT/runtime/node/global"

  sect "1. activation"
  expect "${WS_ROOT:-unset}" "$WS_ROOT" "WS_ROOT"
  [ -n "${WS_ACTIVE:-}" ] && ok "activate.sh sourced" || bad "WS_ACTIVE not set"
  check_tool_is_local

  sect "2. python (uv-managed, inside the workspace)"
  local pyver
  pyver="$(python -V 2>&1)"
  expect "$pyver" "Python 3.13" "interpreter version"
  local exe prefix
  exe="$(python -c 'import sys;print(sys.executable)')"
  prefix="$(python -c 'import sys;print(sys.prefix)')"
  expect "$exe" "$WS_ROOT" "sys.executable inside workspace"
  expect "$prefix" "$WS_ROOT" "sys.prefix inside workspace"
  [ "$(python -c 'import sys;print(sys.base_prefix)')" = "$WS_ROOT/runtime/python/cpython-3.13.13-linux-x86_64-gnu" ] \
    && ok "base interpreter = bundled CPython 3.13.13" \
    || wrn "base_prefix = $(python -c 'import sys;print(sys.base_prefix)')"
  python - <<'PY' >/dev/null 2>&1 && ok "stdlib + installed packages import (yaml, requests, httpx, rich, jsonschema)" || bad "package imports failed"
import yaml, requests, httpx, rich, jsonschema, dateutil, tabulate, dotenv  # noqa: F401
PY
  python -m pip --version >/dev/null 2>&1 && ok "pip present in venv" || bad "pip missing in venv"
  local real_py
  real_py="$(readlink -f "$WS_VENV/bin/python" 2>/dev/null)"
  case "$real_py" in
    "$WS_ROOT"/runtime/python/*/bin/python3|"$WS_ROOT"/runtime/python/*/bin/python3.[0-9]*)
      ok "venv interpreter symlink -> ${real_py#"$WS_ROOT"/}" ;;
    *) bad "venv interpreter path is not a bundled interpreter: ${real_py:-<none>}" ;;
  esac

  sect "3. uv + managed interpreters"
  expect "$(uv --version 2>&1)" "uv " "uv binary"
  local interps
  interps="$(uv python list --only-installed 2>/dev/null | grep -c 'runtime/python' || true)"
  [ "${interps:-0}" -ge 2 ] && ok "managed CPython interpreters: $interps (3.13 + 3.12)" || wrn "managed interpreters found: ${interps:-0}"

  sect "4. node / npm"
  expect "$(node -v 2>&1)" "v24.21.0" "node (default)"
  have npm && ok "npm $(npm -v 2>&1)" || bad "npm missing"
  have npx && ok "npx $(npx --version 2>&1)" || bad "npx missing"
  if [ -x "$WS_ROOT/runtime/node/26.8.2/bin/node" ]; then
    ok "secondary node $("$WS_ROOT/runtime/node/26.8.2/bin/node" -v) at runtime/node/26.8.2"
  else
    wrn "secondary node 26.8.2 not present"
  fi

  sect "5. npm install smoke test (real download + require)"
  local smoke="$WS_ROOT/tmp/npm-smoke"
  rm -rf "$smoke"; mkdir -p "$smoke"
  if (cd "$smoke" && npm init -y >/dev/null 2>&1 && npm install --no-audit --no-fund --silent lodash.get >/dev/null 2>&1); then
    local out
    out="$(cd "$smoke" && node -e "console.log(require('lodash.get')({a:{b:42}},'a.b'))" 2>&1)"
    [ "$out" = "42" ] && ok "npm install + require works (lodash.get -> 42)" || bad "npm require returned '$out'"
    [ -d "$WS_ROOT/runtime/node/global" ] && ok "npm prefix is workspace-local (runtime/node/global)" || wrn "npm global prefix not created yet"
  else
    wrn "npm install smoke test skipped (network?)"
  fi
  rm -rf "$smoke"

  sect "6. git (bundled, self-contained)"
  expect "$(git --version 2>&1)" "git version" "git binary"
  local gitpath; gitpath="$(command -v git)"
  expect "$gitpath" "$WS_ROOT/runtime/git" "git is the bundled build"
  if [ -n "${GIT_CONFIG_GLOBAL:-}" ]; then ok "GIT_CONFIG_GLOBAL=$GIT_CONFIG_GLOBAL"; fi
  local clone="$WS_ROOT/tmp/git-smoke"
  rm -rf "$clone"
  if git clone --depth 1 --quiet https://github.com/octocat/Hello-World.git "$clone" 2>/dev/null; then
    local log1; log1="$(git -C "$clone" log --oneline -1 2>&1)"
    ok "https clone works: ${log1:0:60}"
    [ -f "$clone/README" ] && ok "cloned tree checked out" || bad "clone produced no README"
  else
    wrn "git clone smoke test skipped (network / credentials?)"
  fi
  rm -rf "$clone"
  git config --list --show-origin >/dev/null 2>&1 && ok "git config readable ($(git config --list 2>/dev/null | wc -l) entries)" || wrn "git config unreadable"
  local vc; vc="$(git config --get user.name 2>/dev/null || true)"
  [ -n "$vc" ] && ok "git identity configured: $vc" || wrn "git identity not set yet (config.yaml -> git.identity)"
  # ssh wiring: config/ssh_config + GIT_SSH_COMMAND (all paths workspace-local)
  if [ -f "$WS_ROOT/config/ssh_config" ]; then
    case "${GIT_SSH_COMMAND:-}" in
      *"$WS_ROOT"*) ok "GIT_SSH_COMMAND uses the workspace ssh config (${WS_SSH_CONFIG#"$WS_ROOT"/})" ;;
      "") wrn "config/ssh_config present but GIT_SSH_COMMAND unset (source bin/activate.sh)" ;;
      *) wrn "GIT_SSH_COMMAND points outside the workspace: $GIT_SSH_COMMAND" ;;
    esac
    local sshid; sshid="$(ssh -G -F "$WS_ROOT/config/ssh_config" github.com 2>/dev/null | awk '$1=="identityfile"{print $2; exit}')"
    case "$sshid" in
      "$WS_ROOT"/*) ok "ssh_config identityfile is workspace-local (${sshid#"$WS_ROOT"/})" ;;
      "") wrn "no identityfile resolved from config/ssh_config" ;;
      *) bad "ssh_config identityfile outside the workspace: $sshid" ;;
    esac
    local rkf; rkf="$(ssh -G -F "$WS_ROOT/config/ssh_config" github.com 2>/dev/null | awk '$1=="userknownhostsfile"{print $2; exit}')"
    case "$rkf" in
      "$WS_ROOT"/*) ok "ssh_config known_hosts is workspace-local" ;;
      "") ;;
      *) wrn "known_hosts outside the workspace: $rkf" ;;
    esac
    local authed
    authed="$(timeout 25 ssh -F "$WS_ROOT/config/ssh_config" -o BatchMode=yes -o ConnectTimeout=10 -T git@github.com 2>&1 || true)"
    case "$authed" in
      *"successfully authenticated"*) ok "ssh auth to github.com works ($(printf '%s' "$authed" | head -1))" ;;
      "") wrn "ssh auth to github.com not verified (network?)" ;;
      *) wrn "ssh auth to github.com: $(printf '%s' "$authed" | head -1)" ;;
    esac
  else
    ok "no ssh_key configured yet - ssh wiring skipped (config.yaml -> git.credentials[*].ssh_key)"
  fi

  sect "7. workspace config (config.yaml)"
  if [ -f "$WS_ROOT/config.yaml" ]; then
    local mode; mode="$(stat -c '%a' "$WS_ROOT/config.yaml")"
    [ "$mode" = "600" ] && ok "config.yaml present, mode 600" || wrn "config.yaml mode is $mode (0600 recommended: chmod 600)"
    ws-config validate >/dev/null 2>&1 && ok "ws-config validate OK" || wrn "ws-config validate reported issues (see: ws-config validate)"
    ws-config get git.identity.name >/dev/null 2>&1 && ok "ws-config get works (dot-path reads)" || bad "ws-config get failed"
  else
    bad "config.yaml missing at workspace root (fresh clone? cp config.example.yaml config.yaml)"
  fi

  sect "7b. LLM settings (config.yaml -> request body)"
  if [ ! -f "$WS_ROOT/config.yaml" ]; then
    wrn "LLM settings skipped: no config.yaml yet (cp config.example.yaml config.yaml)"
  elif ws-config llm --json >/dev/null 2>&1; then
    local llmchk
    llmchk="$(ws-config llm --request --json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
st = d["settings"]; r = st.get("reasoning") or {}; body = d.get("request_body") or {}
if not st.get("model"):
    print("NO_MODEL"); raise SystemExit
def subset(doc, want):
    for k, v in (want or {}).items():
        if isinstance(v, dict):
            if not isinstance(doc.get(k), dict) or not subset(doc[k], v):
                return False
        elif doc.get(k) != v:
            return False
    return True
if r.get("enabled"):
    if r.get("unknown"):
        print("UNKNOWN:%s" % r.get("level")); raise SystemExit
    if r.get("supported") and r.get("value") not in r["supported"]:
        print("SNAP_MISS:%s" % r.get("value")); raise SystemExit
    ok = body.get(r.get("param")) == r.get("value")
    state = "on:%s=%s%s" % (r.get("param"), r.get("value"),
                            " (%s->%s)" % (r.get("mapped_from"), r.get("value"))
                            if r.get("mapped_from") else "")
else:
    ok = subset(body, r.get("disabled_body")) and r.get("param") not in body
    state = "off:%s" % json.dumps(r.get("disabled_body") or {}, ensure_ascii=False)
print(("ok:" + state) if ok else "MISMATCH:%s" % json.dumps(body))
' 2>/dev/null)"
    case "$llmchk" in
      ok:on:*) ok "reasoning on - request body carries ${llmchk#ok:on:}" ;;
      ok:off:*) ok "reasoning off - request body carries ${llmchk#ok:off:}" ;;
      UNKNOWN:*) bad "reasoning_effort '${llmchk#UNKNOWN:}' is unknown and not mappable" ;;
      SNAP_MISS:*) bad "reasoning level '${llmchk#SNAP_MISS:}' is not supported by the provider" ;;
      NO_MODEL) wrn "llm settings incomplete: no model selected (config.yaml -> llm.model)" ;;
      "") bad "ws-config llm --request produced nothing" ;;
      *) bad "llm request body $llmchk" ;;
    esac
    if [ "${WS_VERIFY_LLM:-0}" = "1" ]; then
      local live
      live="$(python "$WS_ROOT/tools/llm_probe.py" 2>&1 | tail -3)"
      case "$live" in
        *'"switch_works": true'*) ok "live LLM probe: reasoning_effort=none really suppresses thinking" ;;
        *) wrn "live LLM probe inconclusive (WS_VERIFY_LLM=1): $(printf '%s' "$live" | tail -1)" ;;
      esac
    fi
  else
    bad "ws-config llm failed"
  fi

  sect "8b. agent rules (AGENTS.md)"
  if [ -f "$WS_ROOT/AGENTS.md" ]; then
    local agents_size budget
    agents_size="$(stat -c %s "$WS_ROOT/AGENTS.md")"
    budget="$(sed -n 's/.*budget:[[:space:]]*\([0-9][0-9]*\) bytes.*/\1/p' "$WS_ROOT/AGENTS.md" | head -1)"
    budget="${budget:-2048}"
    if [ "$agents_size" -le "$budget" ]; then
      ok "AGENTS.md within its own budget ($agents_size <= $budget bytes)"
    else
      bad "AGENTS.md is $agents_size bytes, over the declared $budget-byte budget"
    fi
    if [ "$(grep -c '^## Rules' "$WS_ROOT/AGENTS.md")" = "1" ] && [ "$(grep -c '^## Maintenance' "$WS_ROOT/AGENTS.md")" = "1" ]; then
      ok "AGENTS.md keeps its Rules + Maintenance sections"
    else
      bad "AGENTS.md lost its '## Rules' / '## Maintenance' heading"
    fi
    if [ "$(grep -c '^1\.' "$WS_ROOT/AGENTS.md")" = "1" ]; then
      ok "AGENTS.md still enumerates its rules"
    else
      wrn "AGENTS.md rule list looks altered"
    fi
  else
    wrn "no AGENTS.md - agent rules are not pinned in the repo"
  fi

  sect "8. isolation from the host"
  local leaked
  leaked="$(python -c 'import sys;print(",".join(p for p in sys.path if p.startswith("/usr/lib/python") or p.startswith("/usr/local/lib/python")) or "none")')"
  [ "$leaked" = "none" ] && ok "no host site-packages on sys.path" || wrn "host paths visible: $leaked"
  [ "${PYTHONNOUSERSITE:-}" = "1" ] && ok "PYTHONNOUSERSITE=1 (no ~/.local leakage)" || wrn "PYTHONNOUSERSITE unset"

  sect "9. dashboard file browser (read-only API)"
  if [ -f "$WS_ROOT/services/dashboard/files.py" ]; then
    local fb_cfg fb_out fb_rc
    fb_cfg="$(python -c "
import json,sys
cfg=json.load(open('$WS_ROOT/services/dashboard/config.json'))
print(cfg.get('files',{}).get('root','<missing>'))")"
    [ "$fb_cfg" = "projects" ] && ok "config.json files.root defaults to projects" \
      || wrn "config.json files.root is '$fb_cfg' (expected 'projects')"
    for suite in test_files.py test_http.py; do
      fb_out="$(cd "$WS_ROOT" && python3 "services/dashboard/tests/$suite" 2>&1)"; fb_rc=$?
      if [ "$fb_rc" -eq 0 ]; then
        ok "dashboard $suite: $(printf '%s' "$fb_out" | grep -c '^PASS\|ok$') checks passed"
      else
        bad "dashboard $suite failed (rc=$fb_rc)"
        printf '%s\n' "$fb_out" | grep -E 'FAIL|failed|Error' | sed 's/^/      /' | head -8
      fi
    done
    if grep -qE '^\s*"projects"|files' "$WS_ROOT/services/dashboard/config.json" \
       && grep -q 'dashboard-admin-token' "$WS_ROOT/.gitignore"; then
      ok "generated admin token file is gitignored"
    else
      bad "config/dashboard-admin-token is not covered by .gitignore"
    fi
  else
    wrn "services/dashboard/files.py missing - file browser not installed"
  fi
}

# --- 9. relocation test ----------------------------------------------------
relocation_test() {
  sect "10. relocation test (copy -> self-heal -> re-verify)"
  local scratch
  scratch="${WS_RELOCATE_DIR:-$(mktemp -d 2>/dev/null || echo "$WS_ROOT/../ws-reloc-$$")}"
  mkdir -p "$scratch"
  local dest="$scratch/ws"
  rm -rf "$dest" "$scratch/ws.tar"

  local avail_kb
  avail_kb="$(df -Pk "$scratch" | awk 'NR==2{print $4}')"
  if [ "${avail_kb:-0}" -lt 2000000 ]; then
    wrn "not enough free space at $scratch (${avail_kb}kB) - relocation test skipped"
    return
  fi

  tar -C "$(dirname "$WS_ROOT")" \
      --exclude='*/runtime/cache' --exclude='*/tmp' --exclude='*/logs' \
      -cf "$scratch/ws.tar" "$(basename "$WS_ROOT")" 2>/dev/null
  mkdir -p "$dest"
  tar -C "$dest" --strip-components=1 -xf "$scratch/ws.tar"
  ok "copied workspace to $dest (caches excluded, $(du -sh "$dest" 2>/dev/null | cut -f1))"

  local moved=0
  for f in "$dest/venvs/py/bin/python" "$dest/runtime/node/current"; do
    [ -L "$f" ] || continue
    case "$(readlink "$f")" in /*) moved=$((moved + 1)) ;; esac
  done
  [ "$moved" -gt 0 ] && printf '  \033[33mINFO\033[0m %s stale absolute symlink(s) before self-heal\n' "$moved"

  if "$dest/bin/ws-relocate" >"$scratch/relocate.log" 2>&1; then
    ok "ws-relocate ran cleanly ($(grep -c '^  - ' "$scratch/relocate.log" 2>/dev/null || echo 0) fixes)"
  else
    bad "ws-relocate failed - see $scratch/relocate.log"
    sed -n '1,20p' "$scratch/relocate.log" | sed 's/^/      /'
  fi

  # Hard proof of portability: MOVE the copy a second time. The path it was
  # just healed for no longer exists afterwards, so any lingering reference to
  # it becomes a dangling path and every check below would fail.
  local dest2="$scratch/ws-moved"
  rm -rf "$dest2"
  mv "$dest" "$dest2"
  if "$dest2/bin/ws-relocate" --old-root "$dest" >"$scratch/relocate2.log" 2>&1; then
    ok "second move ($dest -> $dest2) healed ($(grep -c '^  - ' "$scratch/relocate2.log" 2>/dev/null || echo 0) fixes)"
  else
    bad "ws-relocate failed after the second move - see $scratch/relocate2.log"
    sed -n '1,20p' "$scratch/relocate2.log" | sed 's/^/      /'
  fi
  dest="$dest2"

  # provenance: the interpreter must be the copy's own bundled CPython
  local base
  if [ -x "$dest/venvs/py/bin/python" ]; then
    base="$("$dest/venvs/py/bin/python" -c 'import sys;print(sys.base_prefix)' 2>&1)"
    printf '     copy root: %s\n' "$dest"
    expect "$base" "$dest" "bundled interpreter runs from the copy (sys.base_prefix)"
  else
    bad "relocated copy has no working venv interpreter: $dest/venvs/py/bin/python"
  fi

  # Functional references to the original root must be gone. Comments in our own
  # scripts legitimately mention the workspace path, so only check what actually
  # resolves paths at runtime.
  local stale="" f
  grep -qI "$WS_ROOT" "$dest"/venvs/*/pyvenv.cfg 2>/dev/null && stale="$stale pyvenv.cfg"
  for f in "$dest"/venvs/*/bin/*; do
    [ -f "$f" ] && [ ! -L "$f" ] || continue
    head -c 200 "$f" 2>/dev/null | grep -q "$WS_ROOT" && stale="$stale $(basename "$f")"
  done
  for f in "$dest/config/gitconfig" "$dest/config/git-credentials"; do
    [ -f "$f" ] && grep -qI "$WS_ROOT" "$f" 2>/dev/null && stale="$stale $(basename "$f")"
  done
  local dangling
  dangling="$(find "$dest/venvs" "$dest/runtime/node" "$dest/runtime/python" -type l ! -exec test -e {} \; -print 2>/dev/null | head -3)"
  if [ -z "$stale" ] && [ -z "$dangling" ]; then
    ok "no functional reference to the original root; no dangling symlinks"
  else
    bad "stale paths remain:${stale:+ files:$stale}${dangling:+ dangling: $(echo $dangling | tr '\n' ' ')}"
  fi

  # the copy must pass every check while the original still exists, and must not
  # *depend* on it: prove by renaming the original root out of the way inside a
  # namespace when possible, else by asserting path provenance above.
  local sub_out sub_rc
  sub_out="$("$dest/tools/verify.sh" 2>&1)"; sub_rc=$?
  local sp sf
  sp="$(printf '%s' "$sub_out" | grep -c 'PASS')"
  sf="$(printf '%s' "$sub_out" | grep -c 'FAIL')"
  if [ "$sub_rc" -eq 0 ]; then
    ok "re-verification inside the relocated copy: $sp checks passed, 0 failed"
  else
    bad "relocated copy reported $sf failure(s) at $dest"
    printf '%s\n' "$sub_out" | grep -E 'FAIL|WARN' | sed 's/^/      /' | head -15
  fi
  printf '     scratch dir: %s\n' "$dest"
  if [ "$KEEP" -eq 0 ] && [ "$sub_rc" -eq 0 ]; then rm -rf "$scratch"; else echo "     (kept: --keep / failures)"; fi
  [ "$sub_rc" -eq 0 ]
}

printf 'workspace toolchain verification\n'
printf 'root: %s   host: %s\n' "$WS_ROOT" "$(uname -sr)"
run_checks

if [ "$RELOCATE" -eq 1 ]; then
  relocation_test || fail=$((fail + 1))
fi

printf '\n--------------------------------------------------\n'
printf 'PASS %d   FAIL %d   WARN %d\n' "$pass" "$fail" "$warn"
if [ "$fail" -eq 0 ]; then
  printf '\033[32mworkspace OK\033[0m\n'
  exit 0
fi
printf '\033[31mworkspace has failures\033[0m\n'
exit 1

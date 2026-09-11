# shellcheck shell=bash
# ---------------------------------------------------------------------------
# <workspace>/bin/activate.sh
#
# Activates the fully self-contained workspace toolchain (python / node / git).
# Every path is derived from this file's own location, so the workspace can be
# moved or bind-mounted anywhere without editing a single line.
#
#   source /workspace/bin/activate.sh
#   ws-shell          # same thing, in a fresh subshell
#
# Prerequisite: none. The only thing it relies on is a POSIX shell + glibc.
# ---------------------------------------------------------------------------

_ws_self="${BASH_SOURCE[0]:-$0}"
WS_ROOT="$(cd -P "$(dirname "$_ws_self")/.." && pwd)"
unset _ws_self
export WS_ROOT

export WS_RUNTIME="$WS_ROOT/runtime"
export WS_BIN="$WS_ROOT/bin"
export WS_TOOLS="$WS_ROOT/tools"
export WS_VENV="$WS_ROOT/venvs/py"
export WS_CONFIG="$WS_ROOT/config.yaml"

# --- python: uv-managed, self-contained CPython -----------------------------
export UV_PYTHON_INSTALL_DIR="$WS_RUNTIME/python"
export UV_CACHE_DIR="$WS_RUNTIME/cache/uv"
export UV_PYTHON_PREFERENCE="${UV_PYTHON_PREFERENCE:-only-managed}"
export UV_LINK_MODE="${UV_LINK_MODE:-copy}"
export PIP_CACHE_DIR="$WS_RUNTIME/cache/pip"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONNOUSERSITE=1

# --- node: local distribution + local prefix/cache --------------------------
export NPM_CONFIG_CACHE="$WS_RUNTIME/cache/npm"
export NPM_CONFIG_PREFIX="$WS_RUNTIME/node/global"
export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false
export NODE_REPL_HISTORY="$WS_RUNTIME/home/.node_repl_history"

# --- XDG: keep caches / config / history inside the workspace ---------------
export XDG_CACHE_HOME="$WS_RUNTIME/home/cache"
export XDG_CONFIG_HOME="$WS_RUNTIME/home/config"
export XDG_DATA_HOME="$WS_RUNTIME/home/data"
export XDG_STATE_HOME="$WS_RUNTIME/home/state"

# --- git: workspace-local global config (identity + credential store) -------
export GIT_CONFIG_GLOBAL="$WS_ROOT/config/gitconfig"
export GIT_CONFIG_NOSYSTEM="${GIT_CONFIG_NOSYSTEM:-1}"
# A bind-mounted workspace usually has a different owner uid than the current
# user, which makes git refuse to touch the repo ("dubious ownership"). Inject
# the exception through the environment instead of writing to $HOME.
_git_cfg_add() {
  local i="${GIT_CONFIG_COUNT:-0}"
  export GIT_CONFIG_COUNT=$((i + 1))
  export "GIT_CONFIG_KEY_$i=$1"
  export "GIT_CONFIG_VALUE_$i=$2"
}
_git_cfg_add safe.directory "$WS_ROOT"
unset -f _git_cfg_add
# The bundled build has the install prefix compiled in for its system config /
# attributes / exec path. Point every one of those at the *current* location so
# git keeps working when the workspace is mounted somewhere else.
if [ -f "$WS_RUNTIME/git/etc/gitconfig" ]; then
  export GIT_CONFIG_SYSTEM="$WS_RUNTIME/git/etc/gitconfig"
fi
# Belt & braces for the bundled build: never rely on prefix paths compiled into
# the git binary, so the workspace stays relocatable even before `ws-relocate`.
if [ -d "$WS_RUNTIME/git/libexec/git-core" ]; then
  export GIT_EXEC_PATH="$WS_RUNTIME/git/libexec/git-core"
fi
if [ -d "$WS_RUNTIME/git/share/git-core/templates" ]; then
  export GIT_TEMPLATE_DIR="$WS_RUNTIME/git/share/git-core/templates"
fi
if [ -f "$WS_RUNTIME/git/ssl/cacert.pem" ]; then
  export GIT_SSL_CAINFO="$WS_RUNTIME/git/ssl/cacert.pem"
  export SSL_CERT_FILE="$WS_RUNTIME/git/ssl/cacert.pem"
fi
if [ -f "$WS_RUNTIME/git/etc/gitattributes" ]; then
  export GIT_ATTR_SYSTEM="$WS_RUNTIME/git/etc/gitattributes"
fi

# --- ssh: workspace-local ssh config for git@ remotes -----------------------
# config/ssh_config is generated from config.yaml (git.credentials[*].ssh_key)
# and uses workspace-local paths only. GIT_SSH_COMMAND makes `git clone
# git@github.com:...` work from anywhere inside the workspace without touching
# $HOME. Regenerated automatically when the workspace was moved/mounted
# elsewhere (the recorded root inside the file no longer matches).
export WS_SSH_CONFIG="$WS_ROOT/config/ssh_config"
export WS_SSH_KNOWN_HOSTS="$WS_ROOT/config/ssh_known_hosts"
if [ -x "$WS_ROOT/bin/ws-config" ]; then
  if ! grep -qF "# root: $WS_ROOT" "$WS_SSH_CONFIG" 2>/dev/null; then
    "$WS_ROOT/bin/ws-config" ssh-setup --quiet >/dev/null 2>&1 || true
  fi
fi
if [ -f "$WS_SSH_CONFIG" ]; then
  export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -F $WS_SSH_CONFIG}"
fi

# --- PATH -------------------------------------------------------------------
_ws_prepend() { case ":$PATH:" in *":$1:"*) ;; *) PATH="$1:$PATH" ;; esac; }
_ws_prepend "$WS_VENV/bin"
_ws_prepend "$WS_RUNTIME/git/bin"
_ws_prepend "$WS_RUNTIME/node/current/bin"
_ws_prepend "$WS_RUNTIME/uv/bin"
_ws_prepend "$WS_BIN"
export PATH
unset -f _ws_prepend

# --- virtualenv (relocatable) ----------------------------------------------
# Self-heal first: if the workspace was moved / bind-mounted at a different path
# since the last activation, repair the absolute paths before using them.
if [ -f "$WS_RUNTIME/.ws-last-root" ]; then
  _ws_recorded="$(cat "$WS_RUNTIME/.ws-last-root" 2>/dev/null)"
  if [ -n "$_ws_recorded" ] && [ "$_ws_recorded" != "$WS_ROOT" ]; then
    "$WS_ROOT/bin/ws-relocate" --quiet || true
  fi
  unset _ws_recorded
fi

if [ -f "$WS_VENV/bin/activate" ]; then
  # shellcheck disable=SC1091
  . "$WS_VENV/bin/activate"
  export VIRTUAL_ENV="$WS_VENV"
fi

# --- optional: export the API keys / secrets from config.yaml on shell start --
# Off by default (config.yaml -> flags.auto_export_api_keys). Cheap when off:
# a single grep, no python startup.
if grep -qE '^[[:space:]]*auto_export_api_keys:[[:space:]]*true' "$WS_ROOT/config.yaml" 2>/dev/null; then
  if [ -x "$WS_ROOT/bin/ws-config" ]; then
    eval "$("$WS_ROOT/bin/ws-config" export 2>/dev/null)" || true
  fi
fi

export WS_ACTIVE=1

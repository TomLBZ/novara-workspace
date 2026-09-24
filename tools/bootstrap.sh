#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# tools/bootstrap.sh - rebuild the whole toolchain from scratch.
#
# You normally do NOT need this: the workspace is self-contained, so on a new
# machine a bind mount + `ws-relocate` is enough. Use bootstrap.sh only to
# rebuild the runtime/ + venvs/ trees (e.g. after a wipe, or on another arch).
#
# Requirements on the host: a POSIX shell, tar, curl, and outbound network to
# astral.sh / nodejs.org / micro.mamba.pm / conda.anaconda.org / pypi.org / github.com.
# No root, no system python required.
#
#   tools/bootstrap.sh            # full rebuild (skips parts already present)
#   tools/bootstrap.sh --force    # wipe runtime/ venvs/ first
# ---------------------------------------------------------------------------
set -euo pipefail

WS_ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

# --- pinned versions --------------------------------------------------------
UV_VERSION="0.11.6"
PYTHON_VERSIONS="3.13 3.12"
NODE_LTS="24.21.0"
NODE_EXTRA="26.8.2"
CODE_SERVER_VERSION="4.138.0"
MICROMAMBA_URL="https://micro.mamba.pm/api/micromamba/linux-64/latest"
GIT_CHANNEL="conda-forge"
BASE_PACKAGES="pyyaml requests httpx rich tabulate jsonschema python-dateutil python-dotenv conda-pack"

say() { printf '\n=== %s ===\n' "$*"; }

if [ "$FORCE" = 1 ]; then
  say "wiping runtime/ venvs/"
  rm -rf "$WS_ROOT/runtime" "$WS_ROOT/venvs"
fi

mkdir -p "$WS_ROOT"/{bin,tools,logs,projects,tmp}
mkdir -p "$WS_ROOT/runtime"/{uv/bin,python,node,git,micromamba/bin,home,cache/uv,cache/npm,cache/pip,cache/dl}

# --- 1. uv ----------------------------------------------------------------
if [ ! -x "$WS_ROOT/runtime/uv/bin/uv" ]; then
  say "installing uv $UV_VERSION"
  if command -v uv >/dev/null 2>&1; then
    cp "$(command -v uv)" "$WS_ROOT/runtime/uv/bin/uv"   # static musl binary
  else
    tmp="$(mktemp -d)"
    curl -fsSL "https://astral.sh/uv/${UV_VERSION}/install.sh" -o "$tmp/uv-install.sh"
    UV_INSTALL_DIR="$tmp/install" UV_NO_MODIFY_PATH=1 sh "$tmp/uv-install.sh" >/dev/null
    cp "$tmp/install/uv" "$WS_ROOT/runtime/uv/bin/uv"
    rm -rf "$tmp"
  fi
  chmod +x "$WS_ROOT/runtime/uv/bin/uv"
fi
export PATH="$WS_ROOT/runtime/uv/bin:$PATH"
export UV_PYTHON_INSTALL_DIR="$WS_ROOT/runtime/python"
export UV_CACHE_DIR="$WS_ROOT/runtime/cache/uv"
export UV_LINK_MODE=copy
uv --version

# --- 2. portable CPython --------------------------------------------------
say "installing CPython $PYTHON_VERSIONS (python-build-standalone via uv)"
uv python install $PYTHON_VERSIONS

# --- 3. relocatable venv + base packages ----------------------------------
say "creating venv venvs/py"
uv venv --relocatable --seed --python 3.13 --python-preference only-managed "$WS_ROOT/venvs/py"
uv pip install --python "$WS_ROOT/venvs/py/bin/python" $BASE_PACKAGES

# --- 4. node ---------------------------------------------------------------
for v in "$NODE_LTS" "$NODE_EXTRA"; do
  if [ ! -x "$WS_ROOT/runtime/node/$v/bin/node" ]; then
    say "installing node v$v"
    cd "$WS_ROOT/runtime/node"
    curl -fsSL --retry 3 -o "node-$v.tar.xz" "https://nodejs.org/dist/v$v/node-v$v-linux-x64.tar.xz"
    tar -xJf "node-$v.tar.xz"
    mv "node-v$v-linux-x64" "$v"
    rm -f "node-$v.tar.xz"
  fi
done
ln -sfn "$NODE_LTS" "$WS_ROOT/runtime/node/current"
mkdir -p "$WS_ROOT/runtime/node/global"

# --- 5. self-contained git (conda-forge build + conda-pack) ---------------
if [ ! -x "$WS_ROOT/runtime/git/bin/git" ]; then
  say "installing micromamba + conda-forge git"
  if [ ! -x "$WS_ROOT/runtime/micromamba/bin/micromamba" ]; then
    curl -fsSL --retry 3 -o "$WS_ROOT/runtime/cache/dl/micromamba.tar.bz2" "$MICROMAMBA_URL"
    "$WS_ROOT/venvs/py/bin/python" - <<PY
import tarfile, pathlib
d = "$WS_ROOT/runtime/cache/dl"
tarfile.open(f"{d}/micromamba.tar.bz2", "r:bz2").extractall(f"{d}/mm")
pathlib.Path("$WS_ROOT/runtime/micromamba/bin").mkdir(parents=True, exist_ok=True)
src = pathlib.Path(f"{d}/mm/bin/micromamba")
src.replace(pathlib.Path("$WS_ROOT/runtime/micromamba/bin/micromamba"))
PY
    chmod +x "$WS_ROOT/runtime/micromamba/bin/micromamba"
    rm -rf "$WS_ROOT/runtime/cache/dl/mm"
  fi
  export MAMBA_ROOT_PREFIX="$WS_ROOT/runtime/cache/mamba"
  "$WS_ROOT/runtime/micromamba/bin/micromamba" create -y \
    -p "$WS_ROOT/runtime/build/git-env" -c "$GIT_CHANNEL" --root-prefix "$MAMBA_ROOT_PREFIX" git
  say "packing git env -> runtime/git (relocatable)"
  "$WS_ROOT/venvs/py/bin/conda-pack" -p "$WS_ROOT/runtime/build/git-env" \
    -o "$WS_ROOT/runtime/cache/dl/git-env.tar.gz" --force
  rm -rf "$WS_ROOT/runtime/git"
  mkdir -p "$WS_ROOT/runtime/git"
  tar -xzf "$WS_ROOT/runtime/cache/dl/git-env.tar.gz" -C "$WS_ROOT/runtime/git"
  "$WS_ROOT/runtime/git/bin/conda-unpack"
  rm -rf "$WS_ROOT/runtime/build"
fi

# --- 6. code-server (VS Code in the browser, `ws-vscode`) ------------------
if [ ! -x "$WS_ROOT/runtime/code-server/$CODE_SERVER_VERSION/bin/code-server" ]; then
  say "installing code-server $CODE_SERVER_VERSION"
  mkdir -p "$WS_ROOT/runtime/code-server" "$WS_ROOT/runtime/cache/dl"
  tarball="code-server-$CODE_SERVER_VERSION-linux-amd64.tar.gz"
  curl -fsSL --retry 3 -o "$WS_ROOT/runtime/cache/dl/$tarball" \
    "https://github.com/coder/code-server/releases/download/v$CODE_SERVER_VERSION/$tarball"
  tar -xzf "$WS_ROOT/runtime/cache/dl/$tarball" -C "$WS_ROOT/runtime/code-server"
  rm -rf "$WS_ROOT/runtime/code-server/$CODE_SERVER_VERSION"
  mv "$WS_ROOT/runtime/code-server/code-server-$CODE_SERVER_VERSION-linux-amd64" \
     "$WS_ROOT/runtime/code-server/$CODE_SERVER_VERSION"
fi
ln -sfn "$CODE_SERVER_VERSION" "$WS_ROOT/runtime/code-server/current"
"$WS_ROOT/runtime/code-server/current/bin/code-server" --version

# --- 7. heal paths + verify ------------------------------------------------
say "relocating + verifying"
"$WS_ROOT/bin/ws-relocate"
"$WS_ROOT/tools/verify.sh"

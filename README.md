# /workspace — self-contained agent toolchain

Everything lives **inside this directory**. Nothing was installed system-wide and nothing
depends on the host beyond a POSIX shell + glibc: copy or bind-mount this tree anywhere and
it still works (see [Portability](#portability--relocation)).

```bash
source /workspace/bin/activate.sh     # put python / node / git / uv on PATH
ws-verify                             # full health check (~10 s)
ws-verify --relocate                  # + copy the tree elsewhere, self-heal, re-verify
```

## Layout

```
/workspace
├── config.yaml              # ← YOUR config: git credentials, identity, API keys (mode 0600)
├── config.example.yaml      #   same schema, safe to commit
├── bin/
│   ├── activate.sh          # source me — sets PATH + all env vars, self-heals paths
│   ├── ws-shell             # open a subshell with the toolchain activated
│   ├── ws-config            # read config.yaml  (show|get|export|env-file|git-setup|validate)
│   ├── ws-relocate          # repair absolute paths after a move/remount
│   └── ws-verify            # health check
├── tools/
│   ├── wsconfig.py          # config library + CLI backend
│   ├── relocate.py          # relocation engine
│   ├── verify.sh            # check suite
│   └── bootstrap.sh         # optional: rebuild runtime/ + venvs/ from scratch
├── runtime/                 # all vendored binaries (no host dependency)
│   ├── uv/bin/uv            #   uv 0.11.6 (static musl)
│   ├── python/              #   CPython 3.13.13 + 3.12.13 (python-build-standalone)
│   ├── node/24.21.0|26.8.2  #   node + npm + npx (current → 24.21.0 LTS)
│   ├── git/                 #   git 2.55.0 + libcurl/openssl/ca-certificates (conda-pack'd)
│   ├── micromamba/bin/      #   micromamba 2.9.0 (escape hatch for more packages)
│   ├── cache/               #   uv / pip / npm caches + offline copies of downloads
│   └── home/                #   XDG cache/config/data, history — kept inside the workspace
├── venvs/py/                # relocatable venv (python 3.13) with base packages
├── projects/                # your work goes here
├── logs/                    # verify logs etc.
└── tmp/                     # scratch
```

## What is installed

| Component | Version | Notes |
|---|---|---|
| Python | 3.13.13 (default) + 3.12.13 | uv-managed, standalone builds, no system python used |
| venv | `venvs/py` | relocatable, seeded pip; pyyaml, requests, httpx, rich, tabulate, jsonschema, python-dateutil, python-dotenv, conda-pack |
| uv | 0.11.6 | package/venv/interpreter manager (`uv pip install`, `uv venv`, `uv python install`) |
| Node.js | v24.21.0 (LTS, default) + v26.8.2 | official linux-x64 builds; `runtime/node/current` switches |
| npm / npx | 11.19.0 | cache + global prefix inside the workspace |
| git | 2.55.0 | bundled with libcurl/openssl/CA bundle → `https://` clones work with no system deps |
| micromamba | 2.9.0 | static; for any extra conda-forge package |

Approximate footprint: `runtime/` ≈ 665 MB, `venvs/` ≈ 21 MB, whole workspace ≈ 685 MB
(the ~112 MB under `runtime/cache/` is re-creatable download cache).

## Environment set by `bin/activate.sh`

`PATH` (workspace bins first), `WS_ROOT`, `WS_RUNTIME`, `WS_VENV`, `VIRTUAL_ENV`,
`UV_PYTHON_INSTALL_DIR`, `UV_CACHE_DIR`, `UV_LINK_MODE=copy`, `PIP_CACHE_DIR`,
`NPM_CONFIG_CACHE`, `NPM_CONFIG_PREFIX`, `XDG_*_HOME` → `runtime/home/*`,
`GIT_CONFIG_GLOBAL=config/gitconfig`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_EXEC_PATH`,
`GIT_TEMPLATE_DIR`, `GIT_SSL_CAINFO`, and `safe.directory` (so git tolerates the
foreign uid of a bind mount — the usual `dubious ownership` failure).

SSH remotes are wired too: `WS_SSH_CONFIG=config/ssh_config`,
`WS_SSH_KNOWN_HOSTS=config/ssh_known_hosts`, and `GIT_SSH_COMMAND=ssh -F config/ssh_config`.
`config/ssh_config` is generated from `config.yaml` (`git.credentials[*].ssh_key`) and points
only at paths inside the workspace, so `git clone git@github.com:you/repo.git` works with no
`-i` flag, no `$HOME/.ssh` and no host configuration. It is regenerated automatically when the
workspace is moved (the `# root:` line no longer matches), which is what makes the ssh setup
survive relocation. `bin/ws-ssh` runs plain `ssh` against the same config
(`ws-ssh -T git@github.com`).

## `config.yaml` — one file for credentials and keys

Schema (see `config.example.yaml` for the annotated version):

```yaml
git:
  identity:   { name: "", email: "" }
  credentials:
    - { id: github, host: github.com, protocol: https, username: "", token: "", ssh_key: "" }
llm:                 # which provider/model agents use, and how hard they think
  provider: deepseek
  fallbacks: []      # optional ordered fallbacks
  model: ""          # empty -> api_keys.<provider>.default_model
  reasoning_effort: medium                  # THE reasoning knob (see below)
  timeout_s: 300
  retries: 2
  # max_tokens: 32000                       # optional cost ceiling
  extra_body: {}
api_keys:
  deepseek:                                 # four fields, same for every provider
    value: ""                               # the secret
    env: DEEPSEEK_API_KEY
    base_url: https://api.deepseek.com/v1
    default_model: deepseek-flash           # ids come from the endpoint, not from a list here
  openai:     { value: "", env: OPENAI_API_KEY,    base_url: "", default_model: "" }
  anthropic:  { value: "", env: ANTHROPIC_API_KEY, base_url: "", default_model: "", kind: anthropic }
  gemini:     { value: "", env: GEMINI_API_KEY,    base_url: "", default_model: "", kind: gemini }
  tavily:     { value: "", env: TAVILY_API_KEY,    base_url: "" }   # non-LLM keys need three fields
secrets: {}          # free-form NAME: value → exported verbatim
proxy:   { http: "", https: "", no_proxy: "" }
```

Usage:

```bash
ws-config validate                       # structure + which fields are still empty
ws-config show                           # whole file, secrets redacted
ws-config get api_keys.openai.value --reveal
ws-config git-setup                      # fill git.credentials → writes config/git-credentials (0600)
                                         #   + config/gitconfig (identity, store helper) — re-run after edits
ws-config ssh-setup                      # git.credentials[*].ssh_key → config/ssh_config (workspace-local paths)
ws-config llm [--json] [--request]       # resolved model / reasoning_effort + request body
ws-config llm --effort none              # override the knob for one query (none = no thinking)
eval "$(ws-config export)"               # export every non-empty api key / secret
ws-config env-file .env                  # or write a chmod-600 .env
python -c "from wsconfig import load, get; print(get(load(),'api_keys.openai.env'))"
```

The file is mode `0600` and listed in `.gitignore` (together with `config/git-credentials`,
`config/gitconfig`, `config/ssh_config`, `config/ssh_known_hosts`, `config/keys/`), so secrets
cannot be committed by accident.

### LLM settings (model, thinking, depth)

`config.yaml` is the single place where "which model, and how hard should it
think" is decided; no code holds a model name.

```bash
ws-config llm                        # provider, kind, model, reasoning_effort, timeout/max_tokens
ws-config llm --effort none          # override for one query: thinking off
ws-config llm --effort xhigh         # shows the value that will really be sent (xhigh -> high)
ws-config llm --request              # the exact chat-completions body that would be sent
ws-config export                     # also emits LLM_PROVIDER / LLM_KIND / LLM_MODEL /
                                     # LLM_REASONING_EFFORT (+ LEVEL) / LLM_TIMEOUT_S
python -c "from wsconfig import llm_settings, build_chat_request; \
           print(build_chat_request([{'role':'user','content':'hi'}], llm_settings()))"
python tools/llm_probe.py            # live A/B: reasoning_effort=none must really stop thinking
```

**There is exactly one reasoning knob: `llm.reasoning_effort`.**

| value | effect on the wire |
|---|---|
| `none` / `null` / empty / key absent | thinking off - the dialect's disabled body is sent instead |
| a level the dialect has (`minimal`, `low`, `medium`, `high`) | sent verbatim as `reasoning_effort` |
| a level it does not have (`xhigh`, `ultra`, ...) | snapped down to the nearest supported level (`--effort xhigh` → `reasoning_effort=high`) |
| an unknown word | falls back to the weakest supported level and `ws-config validate` reports it |

Only the `--effort` argument beats the configured value. **Protocol details are not
config**: a provider block is four fields (`value`, `env`, `base_url`,
`default_model`) plus optional `kind` / `extra_body`, and how the knob is spelled -
which levels exist, what "off" looks like - follows `kind` from a table in
`tools/wsconfig.py` (`KIND_DIALECTS`, default `openai-compatible`). Model ids are
never listed either: they come from the endpoint (`GET /v1/models`), and a wrong id
is reported by the API itself. `temperature` / `top_p` / `max_tokens` are not
configuration knobs any more; the first two have no place in a modern request, and
`max_tokens` exists only as an optional global cost ceiling, and `extra_body`
carries anything else.

**Verified once, then not re-measured.** This endpoint accepts *any* unknown JSON
field with HTTP 200 (a junk parameter returns success), so "the request was
accepted" proves nothing - but that is an argument for one cheap check, not for
sweeping every level. `tools/llm_probe.py` runs a single off/on A/B through the real
request builder and records whether the switch reaches the wire (evidence in
`logs/llm-probe-*.json`); the supported vocabulary itself comes from the API's own
error message:

* supported levels: `minimal`, `low`, `medium`, `high` (from the endpoint's own error message);
  `xhigh`/`ultra` do not exist here and snap to `high`
* effort off is spelled `thinking.type=disabled` and really stops thinking - no `reasoning_content`,
  `reasoning_tokens: null`; confirmed end to end through `build_chat_request`
* `thinking.budget_tokens`, `enable_thinking`, `chat_template_kwargs` → accepted but **silently
  ignored** on this endpoint; do not rely on them
* model ids: `deepseek-flash`, `deepseek-v4-pro`; anything else is rejected with HTTP 400

Per-level token counts are deliberately **not** collected: they change no configuration and cost
money. If you do compare runs, lift `max_tokens` first, or the cap decides the result.

`ws-config validate` resolves the knob through the dialect table, so a level the API does not have
(or an unknown word) is caught instead of sent and silently ignored.

### Git over SSH (no token needed)

Two credential styles are supported and can coexist:

* **HTTPS + PAT** — put the token in `git.credentials[<i>].token`; `ws-config git-setup` renders
  `config/git-credentials` (0600) behind git's `store` helper. GitHub no longer accepts
  passwords, so the token must be a PAT.
* **SSH key** — put the private key path in `git.credentials[<i>].ssh_key` (e.g.
  `config/keys/id_ed25519`, relative paths resolve against the workspace root; the key must be
  mode `0600`) and run `ws-config ssh-setup` (or just source `activate.sh`). Nothing else is
  needed:

  ```bash
  ws-ssh -T git@github.com            # Hi <user>! You've successfully authenticated…
  git clone git@github.com:you/repo.git
  ```

  Pitfall worth knowing: a private key file must end with a **newline** after the
  `-----END …-----` line. Without it OpenSSH refuses the key with the misleading
  `error in libcrypto`, which looks like corruption although every field is intact.

**Pending input:** `git.identity.*`, `git.credentials[*].*`, `api_keys.*` are empty
placeholders — `ws-config validate` lists them. Fill them in and git/API calls start working
immediately; no other change is needed.

## Portability / relocation

The tree contains a handful of unavoidable absolute paths (venv symlinks + `pyvenv.cfg`,
entry-point shebangs, uv key symlinks, npm global shims, and prefix strings compiled into the
bundled git). They all point *inside* the workspace and are repaired by `bin/ws-relocate`,
which:

1. rewrites uv's interpreter symlinks and every venv symlink to relative links,
2. rewrites `pyvenv.cfg` and the shebangs of venv entry points,
3. fixes `runtime/node/current` and npm global shims,
4. runs `conda-unpack` for the bundled git and binary-safe rewrites any remaining prefix
   (NUL-padded, the same technique conda itself uses),
5. records the current root in `runtime/.ws-last-root`.

`activate.sh` calls it automatically when it notices stale paths, so a plain
`source /workspace/bin/activate.sh` is usually enough after a move. On a new machine:

```bash
# host side, nothing to install:
docker run -v /host/workspace:/workspace ...        # or any bind mount / copy
# inside:
source /workspace/bin/activate.sh                   # self-heals, then use python/node/git
ws-verify                                           # confirm 0 FAIL
```

`tools/bootstrap.sh` is only needed to rebuild `runtime/` + `venvs/` from the internet
(it re-downloads uv, CPython, node, conda-forge git; no root, no system python required).

## Publishing: what gets pushed, and what is rebuilt instead

Only the toolchain *source* is versioned. The ~685 MB of runtimes, the venvs and every secret stay
out of the repo (`.gitignore`) and are re-created on the target machine. Measured 2026-09-11 with
`git pack-objects` and a real push into a scratch bare repo:

| what | files | raw | pushed |
|---|---|---|---|
| committed subset (scripts + docs) | 15 | 92.4 KiB | **36.9 KiB** (pack, both commits) |
| whole tree incl. `runtime/`, `venvs/` | 32 137 | 684 MiB | 340.2 MiB (pack) — **rejected by GitHub** |

A naive `git add -A -f` + push cannot land anywhere: GitHub refuses any file above 100 MiB, and two
bundled binaries exceed it (`runtime/node/26.8.2/bin/node` 143.5 MiB, `runtime/node/24.21.0/bin/node`
120.7 MiB). They are also exactly the parts `tools/bootstrap.sh` fetches anyway, so pushing them
would publish one machine's node/CPython/git build for no benefit.

```bash
# once, after the remote exists (the SSH key is already wired)
git remote add origin git@github.com:<user>/<repo>.git
git push -u origin main                 # ~37 KiB of objects

# on the target machine
git clone git@github.com:<user>/<repo>.git ws && cd ws
tools/bootstrap.sh                      # uv + CPython 3.13/3.12 + node 24.21/26.8 + conda-forge git
cp config.example.yaml config.yaml && chmod 600 config.yaml   # re-enter keys + identity
ws-config validate && ws-config git-setup && ws-config ssh-setup
ws-verify                               # 42 PASS / 0 FAIL once config.yaml is filled in
```

The rebuild was verified rather than assumed: `git archive HEAD | tar -x` (those 15 files, 92.4 KiB)
was rebuilt by `tools/bootstrap.sh` into a working toolchain — python 3.13.13, node v24.21.0,
git 2.55.0, uv 0.11.6 — whose `ws-verify` reported **34 PASS / 1 FAIL / 2 WARN**, the single FAIL
being the intentionally absent `config.yaml` (the message now says so and points at
`config.example.yaml`). Network cost per rebuild, pinned versions, no root, no system python:
uv 23.3 MiB + node 62.6 MiB + micromamba 6.7 MiB + conda-forge git env ≈85 MiB + CPython archives
≥17 MiB ≈ **0.2–0.3 GiB**.

**Creating** the remote cannot be done from inside the workspace with its current credentials: an
SSH key can push but never create a repository, and the GitHub API needs a token (`POST /user/repos`
answers `401 Requires authentication`; `gh` is not installed). Create it in the web UI or with a
PAT/`gh auth login`; the `git push` above then works as-is.

## Verification (2026-09-11, Debian 13 · glibc 2.41 · x86_64)

**In place — `ws-verify`: 42 PASS, 0 FAIL, 0 WARN** (full log: `logs/verify-inplace.log`)

- every tool (`python`, `node`, `npm`, `npx`, `git`, `uv`, `ws-config`) resolves *inside* `/workspace`
- python 3.13.13; `sys.executable` + `sys.prefix` inside the workspace; no host site-packages on `sys.path`
- real `npm install` + `require` round-trip (lodash.get → `42`); npm cache/prefix workspace-local
- real `git clone` over https with the bundled git (no system git, no system CA store involved)
- `config.yaml` present, mode `600`, dot-path reads via `ws-config` work
- ssh wiring: `GIT_SSH_COMMAND` + `config/ssh_config` resolve to workspace-local paths, and
  `ssh -T git@github.com` authenticates with the configured key

**Portability — `ws-verify --relocate`: 0 FAIL** (full log: `logs/verify-relocate.log`)

The tree was copied to a *different, longer* path (`/tmp/tmp.XXXX/ws`), self-healed with
`ws-relocate`, then **moved a second time** (`→ ws-moved`) and every check re-run there:

```
copied workspace to /tmp/tmp.XXXX/ws (caches excluded, 376M)
ws-relocate ran cleanly (110 fixes)
second move (/tmp/tmp.XXXX/ws -> ws-moved) healed (109 fixes)
bundled interpreter runs from the copy (sys.base_prefix)  → inside the copy
no functional reference to the original root; no dangling symlinks
re-verification inside the relocated copy: 42 checks passed, 0 failed
total: 48 PASS, 0 FAIL, 0 WARN
```

The ssh wiring travels with the copy: `activate.sh` notices that the `# root:` line inside
`config/ssh_config` no longer matches and regenerates it, so `ssh -T git@github.com` still
authenticates from the relocated tree (part of the 42 checks).

WARNs only ever mean "input still missing": while `git.identity`, `git.credentials[*].ssh_key`
or `api_keys.*` are empty placeholders the corresponding check is skipped instead of failing.
`ws-config validate` lists what is outstanding; fill it in and the WARNs disappear.

### Caveat worth knowing

Binary files in the bundled git have the install prefix compiled in. `ws-relocate` rewrites them
with NUL padding — safe only when the new path is **not longer** than the old one (`/workspace`,
10 chars). Relocating to a longer path is still fully supported for real work: `activate.sh`
exports `GIT_EXEC_PATH`, `GIT_CONFIG_SYSTEM`, `GIT_TEMPLATE_DIR`, `GIT_ATTR_SYSTEM` and the CA
bundle path, so git never falls back to the compiled-in prefix (only cosmetic bits such as
`git help -m` lose their man pages). To have the binaries rewritten as well, bind-mount at a path
of ≤10 characters (e.g. `/workspace`) or re-run `tools/bootstrap.sh`.


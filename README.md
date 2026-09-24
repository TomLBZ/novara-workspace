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
├── AGENTS.md                # ← iron rules for agents working here (budget-checked)
├── config.yaml              # ← YOUR config: git credentials, identity, API keys (mode 0600)
├── config.example.yaml      #   same schema, safe to commit
├── bin/
│   ├── activate.sh          # source me — sets PATH + all env vars, self-heals paths
│   ├── ws-shell             # open a subshell with the toolchain activated
│   ├── ws-config            # read config.yaml  (show|get|export|env-file|git-setup|validate)
│   ├── ws-relocate          # repair absolute paths after a move/remount
│   ├── ws-gateway           # L7 path router control — the one public entry point
│   ├── ws-verify            # health check
│   ├── ws-write             # write a file from stdin despite the Hermes write guard
│   └── ws-plugin-install    # deploy tools/hermes-plugins/* into $HERMES_HOME/plugins
├── tools/
│   ├── wsconfig.py          # config library + CLI backend
│   ├── relocate.py          # relocation engine
│   ├── verify.sh            # check suite
│   ├── llm_probe.py         # one off/on A/B: does the reasoning switch reach the wire?
│   ├── bootstrap.sh         # optional: rebuild runtime/ + venvs/ from scratch
│   └── hermes-plugins/      # Hermes plugin sources (ws-write → ws_write / ws_patch tools)
├── runtime/                 # all vendored binaries (no host dependency)
│   ├── uv/bin/uv            #   uv 0.11.6 (static musl)
│   ├── python/              #   CPython 3.13.13 + 3.12.13 (python-build-standalone)
│   ├── node/24.21.0|26.8.2  #   node + npm + npx (current → 24.21.0 LTS)
│   ├── git/                 #   git 2.55.0 + libcurl/openssl/ca-certificates (conda-pack'd)
│   ├── micromamba/bin/      #   micromamba 2.9.0 (escape hatch for more packages)
│   ├── cache/               #   uv / pip / npm caches + offline copies of downloads
│   └── home/                #   XDG cache/config/data, history — kept inside the workspace
├── venvs/py/                # relocatable venv (python 3.13) with base packages
├── services/                # system-level services (tracked)
│   ├── services.example.json#   service manifest baseline (script/ports/routes/settings)
│   ├── gateway/             #   ws-gateway: the L7 path router (routes come from the manifest)
│   ├── dashboard/           #   ws-dashboard: the ops UI served at /
│   └── sites/hello/         #   the hello-world example, served at /projects/hello
├── projects/                # YOUR projects — each one its own repo, untracked here (rule 7)
├── projects/                # your work goes here
├── logs/                    # verify logs etc.
└── tmp/                     # scratch
```

## `AGENTS.md` — where the rules live

`AGENTS.md` in the workspace root holds the short list of iron rules (no backward
compatibility, no level sweeps for token counts, self-containment, minimal config
surface, finish a batch by pushing) plus the rules for changing the file itself.
It is deliberately the **only** place rule text lives: long-term memory keeps a
one-line pointer to it, README keeps the how-to, skills keep the procedures, and
duplication is what drifts when context gets compacted.

* the file declares its own hard budget (`<!-- budget: 2048 bytes -->`); `ws-verify`
  fails when it grows past it or loses its `## Rules` / `## Maintenance` headings
* it is committed, so every change is a reviewable diff in the history
* only the user edits it; an agent does so solely with explicit approval in the
  current session and must state which rule changed and why

## Repo scope — environment here, projects in their own repos

`AGENTS.md` **rule 7**: this repo tracks the **portable environment** only.

* **tracked** — `bin/`, `tools/`, the system services under `services/` (`gateway` the router,
  `dashboard` the ops UI), `services/sites/hello/` (the example), the `*.example.json` manifests,
  and the docs. A component is system-level when the workspace itself needs it to run; everything
  that belongs to a user project goes to `projects/` instead.
* **untracked, on the bind mount** — `projects/**`, whose members are separate repos, plus
  `services/services.json`. That is the same split as `config.yaml` (machine state, gitignored) vs
  `config.example.yaml` (tracked).
* **bootstrap** — the first `bin/ws-gateway` run copies `services.example.json` → `services.json`.
  The example carries the **system-level baseline** (`gateway` + `dashboard`; dashboard at `/`,
  hello example at `/projects/hello`), so a fresh clone is complete and knows nothing about any
  project. A manifest entry whose script is missing (project repo not cloned on this machine) shows
  as `absent` and is skipped by `ensure`, so the watchdog never couples the repo to a project.
* **adding a service or a project** is one machine-local edit — an entry in
  `services/services.json` (its route, if it needs one, is a `routes` entry on `gateway`) — and zero
  edits here. That file is the single source of service truth:

  ```json
  "gateway":   { "script": "services/gateway/gateway.py", "listen": [80, 8081], "health": "/healthz",
                 "log": "logs/gateway.log",
                 "routes": [ { "prefix": "/", "type": "proxy", "service": "dashboard" } ] },
  "dashboard": { "script": "services/dashboard/dashboard.py", "port": 8090, "health": "/api/health",
                 "log": "logs/dashboard.log",
                 "settings": { } }
  ```

  A proxy route names a **service** (`"service": "dashboard"`) and its port is resolved from that
  entry, or an explicit `"upstream": "http://host:port"` for anything outside the workspace. Ports,
  health paths, logs and settings are never written twice. Credentials, identity and LLM settings
  stay in `config.yaml`; top-level keys starting with `_` in the manifest are comments.
  `ws-gateway validate` checks the file (exit 3 = problems, with the reason and the recovery line),
  `ws-gateway status` prints the services plus the resolved route table.

## Writing files inside `/workspace` (the Hermes write guard)

Hermes guards its `write_file` / `patch` tools with `HERMES_WRITE_SAFE_ROOT` (container default
`/opt/data`), so both refuse paths here. The `terminal` tool is not guarded, and three paths exist —
see the `hermes-write-guard` skill for the full procedure and pitfalls.

| Path | Works | When |
|---|---|---|
| Add `/workspace` to `HERMES_WRITE_SAFE_ROOT` (app env, or a line in `$HERMES_HOME/.env`) | after a gateway restart | the root-cause fix, when the deployment is yours to change |
| `ws-write` / `ws_patch` plugin tools (`bin/ws-plugin-install`) | after a gateway restart | writes should stay auditable as tool calls |
| `bin/ws-write` CLI via `terminal` | immediately | right now, or in a fresh environment before anything is deployed |

```bash
python3 /workspace/bin/ws-write /workspace/notes.md <<'EOF'   # quoted heredoc: no expansion
# content
EOF
printf 'x\n' | ws-write --append /workspace/notes.md            # bin/ is on PATH after activate.sh
ws-write --version
```

`ws-write` refuses an empty stdin (`--allow-empty` overrides), creates parent directories, and replaces
the file atomically through a same-directory temp file. That refusal is deliberate: a shell that eats a
heredoc body would otherwise silently truncate the target to 0 bytes.

`tools/hermes-plugins/<name>/` is the canonical source of the plugins; `$HERMES_HOME/plugins/<name>` is a
deployment of it, the same way `runtime/` is rebuilt rather than committed. Re-run `ws-plugin-install`
after any plugin change, then restart the gateway to load it.

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

## Public egress — `ws-gateway` behind nginx-proxy-manager

Exactly one public address is approved for this workspace: **`novara.remoteblossom.com`**.
Nginx Proxy Manager (container `ix-nginx-proxy-manager-npm-1`, on the shared docker network
as `172.16.1.2`) forwards that domain to this container's router, which turns one entry point
into many services by **path prefix**:

```
internet -> Cloudflare -> cloud-server NPM -> ZeroTier -> host-machine NPM -> docker -> ws-gateway -> local services
                                (public wildcard)      (10.147.17.100)   (*.local.remoteblossom.com)
```

Verified hops (2026-09-11): Cloudflare -> cloud NPM is the public leg; the host machine is **not**
exposed publicly (its ZeroTier address is `10.147.17.100`). The host's NPM holds **two vhosts and two
certificates** — `novara.remoteblossom.com` with `*.remoteblossom.com` and
`novara.local.remoteblossom.com` with `*.local.remoteblossom.com` — both forwarding to `ws-gateway`.

Invariants for the proxy -> proxy hop (measured, not guessed):

- **`https` upstream, port 443** — the host answers `301` on `:80`.
- **SNI is mandatory and must name a vhost on the host**: nginx with no SNI gets the host's default
  `:443` server, which runs `ssl_reject_handshake` and answers the `unrecognized_name` alert
  (surfacing as a ~25 ms `502` at the public proxy). NPM does not send an upstream SNI by default,
  so the cloud proxy needs exactly two advanced lines:
  `proxy_ssl_server_name on;` and `proxy_ssl_name novara.remoteblossom.com;`
  (`proxy_ssl_verify` defaults to off; add `proxy_ssl_verify on;` too now that the host presents a
  publicly trusted wildcard, optionally with `proxy_ssl_trusted_certificate` + `proxy_ssl_verify_depth 2`).
- **SNI and `Host` must land on the same vhost**: TLS picks the certificate by SNI, the HTTP layer
  then picks the vhost by `Host` (the cloud proxy forwards `Host: $host`). A `Host` that matches
  nothing is answered by a **zero-byte connection close**, not a 404 — also a 502.
- Keeping the `.local.` client path intact means *adding* a vhost, never swapping the certificate on
  the existing one: `*.remoteblossom.com` does not cover `novara.local.remoteblossom.com` (a
  wildcard matches exactly one label), so a swapped certificate breaks every internal user.

The live table (2026-09-11) — the dashboard owns `/`, projects live under a prefix of their own:

```
/projects/hello   static  services/sites/hello     hello-world project
/                 proxy   http://127.0.0.1:8090    ws-dashboard (UI + /api/status + /api/files/*)
```

`/healthz` is reserved by the router, and both listen ports carry the same table, so NPM can forward
to either one. Route matching is longest-prefix; `strip_prefix` decides whether the prefix is
removed before forwarding. A proxy route adds `"websocket": true` to carry WebSocket upgrades on its
prefix; without it an upgrade request on that prefix is refused with `400`. The table lives in the
manifest (`services.json` → `gateway.routes`), so after editing it run
`ws-gateway validate && ws-gateway restart gateway` (it is read at start-up).

**Services.** The machine-local `services/services.json` (bootstrapped from the tracked example) is
the manifest of everything that must stay up; each entry declares `script`, a port (`port`, or
`listen` for the multi-port router), `health` and `log`, and every service implements `--healthz PORT`
(exit 0 when it answers its health path). `ws-gateway status` shows them all plus the resolved route
table, `ws-gateway validate` checks the manifest, `ws-gateway start|stop|restart [SERVICE ...]`
controls one or all, and `ensure` starts whatever is down — which is what the cron watchdog calls.
`ws-dashboard` takes its port and settings from its manifest entry (it has no file of its own) and
renders services, routes, watchers, container stats and the last requests; `/api/status` is the
machine-readable version of the same view and `--check` prints the entry it resolved.

**File browser.** `ws-dashboard` also serves a **read-only** file browser (`/api/files/access|list|read|raw`,
UI card "Files"; implementation `services/dashboard/files.py`). Two scopes: without a token it browses
`files.root` (default `projects`); pasting the admin token widens it to the workspace root
(`files.admin_root`). Secrets stay unreadable in **both** scopes - `config.yaml`, `config/keys/**`,
`.git/**`, `.env*`, `*.pem`, `*.key`, `id_rsa*` - and every request resolves under its scope root, so
`..`, absolute paths and symlinks cannot escape. Nothing is ever written.

The token is read from `config.yaml` -> `dashboard.admin_token` when set; otherwise it is generated on
first start into `config/dashboard-admin-token` (mode 0600, gitignored). Reveal or rotate it with

```bash
services/dashboard/dashboard.py --files-token            # print it (generates on first call)
services/dashboard/dashboard.py --files-token --rotate   # new token, old one dies at once
```

Five wrong tokens from one client lock the unlock endpoint for 5 minutes. Knobs live in the manifest
under `dashboard.settings.files` (`root`, `admin_root`, `token_file`, `max_entries`,
`max_preview_bytes`, `max_raw_bytes`, `deny_extra`).

**Surviving a new image.** Every service lives on the bind-mounted workspace (`services/`,
`bin/ws-gateway`) and runs on the workspace's own venv python, so a container rebuilt from a
different image restores the public entry point without image changes: the Hermes cron job
**`ws-gateway watchdog`** (every minute, no LLM) runs `ws-gateway ensure`, bringing everything back
within ~60 s and staying silent while it is healthy. Its glue script
(`$HERMES_HOME/scripts/ws-gateway-ensure.sh`) is deployment wiring, deliberately outside the
workspace.

**Transport.** Responses stream: a proxied body is copied to the client as it arrives, keeping the
upstream's framing (`Content-Length` relayed as-is, a chunked upstream re-framed as chunked, an
upstream that says nothing about length becomes `Connection: close`), and request bodies stream too —
`Content-Length` and `Transfer-Encoding: chunked` alike. WebSocket upgrades tunnel on the routes that
opt in with `"websocket": true`: the RFC 6455 handshake is validated (`Sec-WebSocket-Version: 13` plus
a 16-byte `Sec-WebSocket-Key`), relayed to the upstream verbatim, and both directions are then spliced
byte-for-byte — no re-framing, so control frames and 64-bit lengths survive. A tunnel idle for 15
minutes is closed, and every open, close (with byte counts) and refusal is logged. On a prefix that did
not opt in, an upgrade is `400`, so the router is never a general-purpose TCP relay. `tools/verify.sh`
runs the two suites that pin this down: `services/gateway/tests/test_http.py` (framing, streaming,
request bodies) and `services/gateway/tests/test_websocket.py` (handshake, splice, refusals).

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
ws-verify                               # 45 PASS / 0 FAIL once config.yaml is filled in
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

**In place — `ws-verify`: 45 PASS, 0 FAIL, 0 WARN** (full log: `logs/verify-inplace.log`)

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
re-verification inside the relocated copy: 45 checks passed, 0 failed
total: 51 PASS, 0 FAIL, 0 WARN
```

The ssh wiring travels with the copy: `activate.sh` notices that the `# root:` line inside
`config/ssh_config` no longer matches and regenerates it, so `ssh -T git@github.com` still
authenticates from the relocated tree (part of the 45 checks).

WARNs only ever mean "input still missing": while `git.identity`, `git.credentials[*].ssh_key`
or `api_keys.*` are empty placeholders the corresponding check is skipped instead of failing.
`ws-config validate` lists what is outstanding; fill it in and the WARNs disappear.

**Public egress — `ws-gateway` behind nginx-proxy-manager** (2026-09-11)

- the router serves the hello site on `:80` *and* `:8081` inside the container:
  `curl -H 'Host: novara.remoteblossom.com' http://<container-ip>/` → `200`
  (`<title>Hello from the Hermes workspace</title>`), `/healthz` → `{"status": "ok"}`
- proxy branch verified against a throwaway upstream on `127.0.0.1:8099`: `/probe/` → `200`,
  `/probe/missing.html` → `404` (status and body passed through)
- watchdog proven end to end: router killed at `20:36:57Z`, back up at `20:37:49Z` — the
  per-minute cron job restored it in 52 s without any image-side change
- **dashboard live (2026-09-11 21:54Z)**: `https://novara.remoteblossom.com/` → `200` (rendered in a
  real browser: services up, watcher states, container stats, 60 ms browser round trip),
  `/api/status` → `200` JSON, `/projects/hello/` → `200` (hello page, still wired to `/healthz`),
  `/assets/*` → `200`, unknown path → `404`
- **Cloudflare Rocket Loader rewrites `<script>` tags** on this zone (it rewrote
  `/assets/app.js` and deferred the inline script). Both pages carry `data-cfasync="false"` to opt
  out; turning Rocket Loader off for the zone is the cleaner fix
- **public leg live (2026-09-11 21:32Z)**: `https://novara.remoteblossom.com/healthz` → `200` with
  this workspace's JSON in 0.40 s, `/` → `200` (hello page), `http://` → `301 https`, and the
  ZeroTier-side name `novara.local.remoteblossom.com` still returns `200` in parallel — the two
  names coexist because the SNI picks the certificate and the `Host` picks the vhost
- the public leg failed earlier with an instant `502` (0.025 s), which was **not** the workspace: the
  cloud proxy sent `Host: novara.remoteblossom.com` while the host's vhost only matched
  `novara.local.remoteblossom.com`, and an unmatched Host on the host's `:443` is answered by a
  zero-byte connection close, not a 404

### Caveat worth knowing

Binary files in the bundled git have the install prefix compiled in. `ws-relocate` rewrites them
with NUL padding — safe only when the new path is **not longer** than the old one (`/workspace`,
10 chars). Relocating to a longer path is still fully supported for real work: `activate.sh`
exports `GIT_EXEC_PATH`, `GIT_CONFIG_SYSTEM`, `GIT_TEMPLATE_DIR`, `GIT_ATTR_SYSTEM` and the CA
bundle path, so git never falls back to the compiled-in prefix (only cosmetic bits such as
`git help -m` lose their man pages). To have the binaries rewritten as well, bind-mount at a path
of ≤10 characters (e.g. `/workspace`) or re-run `tools/bootstrap.sh`.


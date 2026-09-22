# novara dashboard

Operations dashboard for the novara workspace: the page served at
`https://novara.remoteblossom.com/`. It is a **system-level service** and therefore tracked by this
repo (`AGENTS.md` rule 7) — components that belong to a user project live under `projects/` in their
own repos instead.

* `dashboard.py` — stdlib HTTP app: serves the UI, `/api/status` (services, projects, routes, watchers,
  container stats, recent requests), `/api/health` and the `/api/files/*` file browser; implements
  `--healthz PORT`.
* `files.py` — the read-only file-browser backend (scopes, deny list, previews, admin token).
* no config file of its own — port, health path and settings come from its entry in
  `/workspace/services/services.json` (`tools/servicemanifest.py` is the reader/validator).
* `static/` — `index.html`, `app.css`, `app.js` (no build step, no CDN; `data-cfasync="false"`
  opts out of Cloudflare Rocket Loader).
* `tests/` — `test_files.py` (backend units) and `test_http.py` (end-to-end over a scratch port);
  `tools/verify.sh` runs both.

## Services vs projects & routes (the wiring)

Two different things live on the page, and each has exactly one source:

* **Services** — one row per manifest entry: name, `pid`, ports, `url`, health and a one-line
  `description`. A service's `links` array is **intentionally empty** (the key is kept so older
  readers don't break): project sub-pages do not belong in the service list. Endpoint links
  (`api_links`) stay, but the UI tucks them into a collapsed `<details>` — detail, not the list's
  main visual, and the reason an endpoint list is empty is printed (`links_source`:
  `service:/api/routes` / `routes-unreachable` / `routes-not-json` / `no-prefix` / `none`).
* **Projects & routes** — one **route entry per independently running webui app**
  (`projects[]` in `/api/status`): its own name, its entry prefix (e.g. `/quotagent/`), an
  "独立 app" badge, plus its sub-pages and endpoints as collapsible secondary links. Below it, the
  router's own route table (`routes[]`, from the manifest).

`projects[]` is **derived, never hardcoded**: a candidate is any service with its own gateway
prefix (≠ `/`), and the entry's name/prefix come from that service's **own** self-declaration in
`GET <prefix>/api/routes` — e.g. quotagent answers `{"service": "quotagent-webui", "route_prefix":
"/quotagent", "source": "host/modules/webui.mjs", "routes": [...]}`. `entry_source` / `name_source`
record who supplied each value; a service that cannot be reached still gets an entry, but with
`kind: "unverified"` and `routes_source` naming the reason — the dashboard reports, it does not guess.
Adding a webui app therefore needs no dashboard change: register it in the manifest with its own
prefix and have it answer `/api/routes`.

New `/api/status` service fields: `description`, `endpoints` (all routes the service declared),
`app` (the service's own self-declaration). New top-level field: `projects`. Removed/renamed fields:
none.

## File browser (read-only)

| endpoint | method | what it does |
|---|---|---|
| `/api/files/access` | GET | current scope, browse root, caps |
| `/api/files/list?path=<rel>` | GET | one directory level (dirs first, sizes, mtimes, `denied` flags) |
| `/api/files/read?path=<rel>&max=<n>` | GET | text preview (capped, `truncated` flag) or a `binary` marker |
| `/api/files/raw?path=<rel>` | GET | inline bytes for small whitelisted types (png/jpg/gif/webp/svg/ico/pdf) |
| `/api/files/unlock` | POST | `{"token": "..."}` → verifies the admin token |

**Scopes.** Anonymous callers browse `files.root` (default `projects`). The admin token widens the
scope to `files.admin_root` (default `.`, the workspace root) and is sent as the `X-WS-Files-Token`
header (or `Authorization: Bearer ...`) — never in the URL, so it cannot leak into access logs,
referrers or browser history. The UI keeps it in `localStorage` and clears it on any `401`.

**Safety.** Every request resolves to a real path under its scope root, so `..`, absolute paths and
symlinks cannot escape. A code-level deny list holds in both scopes: `config.yaml`,
`config/keys/**`, `config/git-credentials`, `.git/**`, `.ssh/**`, `.env*`, `*.pem`, `*.key`,
`*.p12`, `*.pfx`, `id_rsa*`, `id_ed25519*`. Denied entries are still listed in the admin scope but
marked `denied: true` and are not readable. The API is read-only — nothing under it writes a file.
Five wrong tokens from one client lock `/api/files/unlock` and the token-authenticated reads for
300 s.

**Token.** Read from `config.yaml` → `dashboard.admin_token` when set; otherwise generated on first
start into `config/dashboard-admin-token` (mode 0600, gitignored).

```bash
services/dashboard/dashboard.py --files-token            # print it (generates on first call)
services/dashboard/dashboard.py --files-token --rotate   # rotate (the old token stops working)
ws-config get dashboard.admin_token --reveal             # if you pinned one in config.yaml
```

Knobs live in `services/services.json` under `dashboard.settings` (`public_url`, `refresh_seconds`,
`files{root, admin_root, token_file, max_entries, max_preview_bytes, max_raw_bytes, deny_extra}`).
Env: `WS_SERVICE` (which manifest entry is me, default `dashboard`), `WS_MANIFEST` (alternate manifest
file — how the tests stay off the live one) and `WS_PID_FILE`. `dashboard.py --check` prints the entry
it resolved; `--files-token [--rotate]` prints/rotates the file-browser token.

## Running it

Registering it on a machine is one entry in the workspace's service manifest
(`/workspace/services/services.json`):

```json
"dashboard": {
  "script": "services/dashboard/dashboard.py",
  "port": 8090,
  "health": "/api/health",
  "log": "logs/dashboard.log",
  "settings": { "public_url": "https://…", "refresh_seconds": 5, "files": { "root": "projects" } }
}
```

then `ws-gateway validate && ws-gateway restart dashboard`. The public path is a route on the same
file's `gateway` entry (`{ "prefix": "/", "type": "proxy", "service": "dashboard" }` — the upstream
`http://127.0.0.1:8090` is resolved from the port above, so the port is written once). Restart the
service after changing `dashboard.py`, `files.py` or the static files (Python modules are read at
start-up, static assets per request).

Checks: `python3 services/dashboard/tests/test_files.py` and
`python3 services/dashboard/tests/test_http.py` (or simply `tools/verify.sh`).

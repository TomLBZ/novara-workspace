# novara dashboard

Operations dashboard for the novara workspace: the page served at
`https://novara.remoteblossom.com/`. It is a **system-level service** and therefore tracked by this
repo (`AGENTS.md` rule 7) — components that belong to a user project live under `projects/` in their
own repos instead.

* `dashboard.py` — stdlib HTTP app: serves the UI, `/api/status` (services, routes, watchers,
  container stats, recent requests), `/api/health` and the `/api/files/*` file browser; implements
  `--healthz PORT`.
* `files.py` — the read-only file-browser backend (scopes, deny list, previews, admin token).
* no config file of its own — port, health path and settings come from its entry in
  `/workspace/services/services.json` (`tools/servicemanifest.py` is the reader/validator).
* `static/` — `index.html`, `app.css`, `app.js` (no build step, no CDN; `data-cfasync="false"`
  opts out of Cloudflare Rocket Loader).
* `tests/` — `test_files.py` (backend units) and `test_http.py` (end-to-end over a scratch port);
  `tools/verify.sh` runs both.

## Services vs. projects & routes (what goes where)

Two different things, deliberately kept apart — and both are **minimal by rule**:

* **`services`** — each row is only a *service*: `name`, `pid`, `ports`, `script`, `healthy`, `log`.
  That is the whole field set, and it is the pre-`ac0d9b2` shape. A service row carries **no `url`,
  no `links`/`api_links`, no page or endpoint lists and no `<details>` expansion**: "services" is not
  "routes", so it must not advertise any path into a project.
* **`projects & routes`** — each row is **one project's route entry**, i.e. the front door of an app
  that runs on its own (`/quotagent` → `/quotagent/`). Rows use only this block's own fields
  (`prefix`, `type`, `target`, `link`, plus `entry_source`, which exists purely to report where the
  entry came from). There is **no `projects[]` structure, no widget/card, no sub-page list and no
  `/api/*` list** here: an app's sub-pages are the app's own responsibility — *a site's entry point
  must never treat every sub-page as an entry point*.

**The one entry declaration.** A route's clickable entry path comes from exactly one source, in this
order:

1. the app behind the route declares it itself — the **first** route in its
   `GET <prefix>/api/routes` whose `path` ends in `/` and equals the prefix itself
   (`/quotagent` → `/quotagent/`, from `quotagent-webui`'s own route table). Only that one route is
   read; the rest of that table (sub-pages, `/api/*`) is never pulled into this layer;
2. the route declares it itself — `gateway.routes[].entry` (`/vscode` → `/vscode/`). This is for an
   app that cannot answer a route table at all: `/vscode` is code-server, a vendored third-party
   binary behind a supervisor (`services/vscode/vscode.py`), so its front door is a fact the *route*
   knows rather than something this layer could infer;
3. a route with **no service behind it** (e.g. the `static` route `/projects/hello`) has nothing to
   ask, so the router's own `gateway.routes.prefix` is the only declaration.

Nothing is inferred and nothing is invented: if the app is reachable but has no route table of its
own (`no-route-table`, e.g. the dashboard itself), answers non-JSON (`routes-not-json`), does not
declare its prefix (`entry-not-declared`) or does not answer at all (`routes-unreachable`) **and** the
route declares no `entry` of its own, the row's `link` stays `null` — the block never falls back to a
guessed path — and the page shows the bare prefix as plain text with that reason in its tooltip.

## UI components (`static/`)

No build step and no CDN (`data-cfasync="false"` opts out of Cloudflare Rocket Loader). A cell that
shows the same kind of information as another cell goes through the **same component** in `app.js`'s
`ui` namespace, so one fact can never appear in two vocabularies:

* `ui.chip(text, {tone, title})` — the small pill for a single attribute value;
* `ui.schedule(s)` — a watcher's period, from the API's structured `schedule`
  (`{kind, every_seconds, raw, display}`): a cron job and an interval job both end up saying the same
  thing (`every 5 min`), with their own expression in the tooltip; a schedule that cannot be read as
  an interval keeps its original wording (`tone: "dim"`) instead of being given an invented period;
* `ui.status(value)` — a watcher's last outcome.

Explaining a panel belongs in this README, not in prose on the page: the UI shows data, this file
says what it means and where it comes from.

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

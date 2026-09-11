# novara dashboard

Operations dashboard for the novara workspace: the page served at
`https://novara.remoteblossom.com/`. It is a **system-level service** and therefore tracked by this
repo (`AGENTS.md` rule 7) — components that belong to a user project live under `projects/` in their
own repos instead.

* `dashboard.py` — stdlib HTTP app: serves the UI, `/api/status` (services, routes, watchers,
  container stats, recent requests) and `/api/health`; implements `--healthz PORT`.
* `config.json` — `listen_host`, `listen_port`, `public_url`, `refresh_seconds`.
* `static/` — `index.html`, `app.css`, `app.js` (no build step, no CDN; `data-cfasync="false"`
  opts out of Cloudflare Rocket Loader).

Registering it on a machine is one entry in the workspace's service manifest
(`/workspace/services/services.json`):

```json
"dashboard": {
  "script": "projects/dashboard/dashboard.py",
  "probe_ports": [8090],
  "health": "/api/health",
  "log": "logs/dashboard.log"
}
```

then `ws-gateway restart dashboard`. The path prefix that exposes it publicly lives in
`/workspace/services/gateway/routes.json` (`/` → `http://127.0.0.1:8090`).

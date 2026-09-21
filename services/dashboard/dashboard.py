#!/usr/bin/env python3
"""ws-dashboard - the workspace's operations dashboard (stdlib only).

Serves the UI plus a small JSON API and is reached from the public entry point
through ws-gateway (a proxy route that names this service). Everything it needs -
port, health path, and its own settings - comes from the service manifest
(`services/services.json` -> the `dashboard` entry; tracked baseline
`services/services.example.json`). Credentials live in `config.yaml`, never here.

  /                UI (static/index.html)
  /assets/<f>      static assets
  /api/status      aggregated live JSON
  /api/health      {"status": "ok"}   <- shared service contract
  /api/files/*     read-only file browser (files.py): access, list, read, raw, unlock

Every workspace service implements `--healthz PORT`: exit 0 when it answers its
health path on that port.  bin/ws-gateway uses that to keep services alive.
"""
from __future__ import annotations

import http.server
import json
import os
import pathlib
import platform
import socket
import socketserver
import sys
import time
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
WS_ROOT = HERE.parents[1]
sys.path.insert(0, str(WS_ROOT / "tools"))
import servicemanifest as sm  # noqa: E402  (workspace tool, stdlib only)

SERVICE = os.environ.get("WS_SERVICE", "dashboard")
try:
    MANIFEST = sm.load(WS_ROOT)
    ENTRY = sm.entry(MANIFEST, SERVICE)
    CFG = sm.settings(MANIFEST, SERVICE)
    _PROBLEMS = sm.problems(MANIFEST)
except sm.ManifestError as exc:
    print("ws-dashboard: %s" % exc, file=sys.stderr)
    raise SystemExit(3)
if _PROBLEMS:
    print("ws-dashboard: services/services.json is invalid:\n  - " + "\n  - ".join(_PROBLEMS), file=sys.stderr)
    raise SystemExit(3)
sys.path.insert(0, str(HERE))
from files import FileBrowser  # noqa: E402  (same directory, stdlib-only)

BROWSER = FileBrowser(WS_ROOT, CFG.get("files", {}))
STATIC = HERE / "static"
PID_FILE = pathlib.Path(os.environ.get("WS_PID_FILE", str(WS_ROOT / "runtime" / "run" / "dashboard.pid")))
STARTED = time.time()


_MANIFEST_CACHE = {"mtime": None, "data": MANIFEST}


def manifest() -> dict:
    """清单按 mtime 重读。

    实测根因：dashboard 进程 9/16 启动、9/21 才在 services.json 里加 quotagent →
    模块级 MANIFEST 永不重读 → 新服务**永远**不出现在 dashboard 上（"不存在"而不是"变红"）。
    坏清单不覆盖好清单：宁可继续显示旧的，也不要整页空掉。
    """
    path = sm.path(WS_ROOT)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _MANIFEST_CACHE["data"]
    if mtime != _MANIFEST_CACHE["mtime"]:
        try:
            data = sm.load(WS_ROOT)
            if not sm.problems(data):
                _MANIFEST_CACHE.update(mtime=mtime, data=data)
        except sm.ManifestError:
            pass
    return _MANIFEST_CACHE["data"]


def service_links(m: dict) -> dict:
    """服务名 → 经网关可达的相对路径（第一条把该服务当目标的代理路由）。"""
    out = {}
    for route in (m.get("gateway", {}) or {}).get("routes") or []:
        if isinstance(route, dict) and route.get("type") == "proxy" and route.get("service"):
            prefix = str(route.get("prefix") or "/")
            out.setdefault(str(route["service"]), prefix if prefix.endswith("/") else prefix + "/")
    return out


def read_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def probe(port: int, path: str = "/healthz", timeout: float = 2.0):
    """Return (ok, body) for a plain HTTP GET on loopback."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as s:
            s.sendall(f"GET {path} HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        text = data.decode("utf-8", "replace")
        body = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else ""
        return ("200" in text.split("\r\n", 1)[0], body)
    except Exception as exc:
        return (False, str(exc))


def collect_services() -> list:
    out = []
    m = manifest()
    links = service_links(m)
    for name in sm.services(m):
        spec = m.get(name) or {}
        ports = sm.ports(spec)
        pid_file = WS_ROOT / "runtime" / "run" / f"{name}.pid"
        pid, health = None, False
        try:
            pid = int(pid_file.read_text().strip())
        except Exception:
            pid = None
        for port in ports:
            ok, body = probe(port, spec.get("health", "/healthz"))
            if ok:
                health = True
                break
        out.append({
            "name": name,
            "pid": pid if pid and _alive(pid) else None,
            "ports": ports,
            "script": spec.get("script", ""),
            "healthy": health,
            "log": spec.get("log", ""),
            "url": links.get(name),      # 经网关可达的相对路径（dashboard 上可点，不再是纯文本）
            # 该服务自己声明的子路由（如 quotagent 的双方视角/运维/系统管理）——
            # 用户要求"不同 routes 提供双方各自可见的 UI，而不是只有一条 dashboard route"
            "links": [dict(x) for x in (spec.get("settings", {}) or {}).get("subroutes", [])
                      if isinstance(x, dict) and x.get("label") and x.get("path")],
        })
    return out


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def collect_routes() -> list:
    """The router's route table, resolved from the manifest (a `service` target -> its port)."""
    try:
        routes = sm.routes(MANIFEST)
    except (sm.ManifestError, KeyError):
        routes = []
    rows = []
    for r in routes:
        prefix = r.get("prefix", "/")
        rows.append({
            "prefix": prefix,
            "type": r.get("type"),
            "target": r.get("root") or r.get("upstream"),
            "link": prefix if prefix.endswith("/") else prefix + "/",
        })
    return rows


def collect_system() -> dict:
    load = pathlib.Path("/proc/loadavg").read_text().split()[:3]
    up = float(pathlib.Path("/proc/uptime").read_text().split()[0])
    mem = {}
    for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = round(int(v.split()[0]) / 1024)
    vfs = os.statvfs(WS_ROOT)
    git_rev = None
    try:
        head = (WS_ROOT / ".git" / "HEAD").read_text().strip()
        ref = head.split(" ", 1)[1] if head.startswith("ref:") else None
        git_rev = (WS_ROOT / ".git" / ref).read_text().strip()[:8] if ref else head[:8]
    except Exception:
        pass
    return {
        "container": socket.gethostname(),
        "python": platform.python_version(),
        "kind": f"{platform.system()} {platform.release()}",
        "uptime_h": round(up / 3600.0, 2),
        "load": load,
        "mem_total_mb": mem.get("MemTotal"),
        "mem_avail_mb": mem.get("MemAvailable"),
        "workspace_gb_free": round(vfs.f_bavail * vfs.f_frsize / 1e9, 1),
        "workspace_gb_total": round(vfs.f_blocks * vfs.f_frsize / 1e9, 1),
        "git_rev": git_rev,
    }


def collect_watchers() -> list:
    home = pathlib.Path(os.environ.get("HERMES_HOME", "/opt/data"))
    store = read_json(home / "cron" / "jobs.json") or {}
    rows = []
    for job in store.get("jobs", []):
        sched = job.get("schedule_display")
        raw = job.get("schedule")
        if not sched:
            sched = (raw.get("display") or raw.get("expr")) if isinstance(raw, dict) else raw
        last = job.get("last_run_at") or ""
        if "T" in last:                      # 2026-09-11T21:52:20.176289+00:00 -> 21:52:20Z
            last = last.split("T", 1)[1][:8] + "Z"
        rows.append({
            "name": job.get("name") or job.get("id"),
            "schedule": sched or "—",
            "last_run_at": last or "—",
            "last_status": job.get("last_status") or "pending",
            "enabled": job.get("enabled", True),
        })
    return rows


def collect_requests(limit: int = 12) -> list:
    log = WS_ROOT / "logs" / "gateway.log"
    try:
        lines = log.read_text(errors="replace").splitlines()
    except Exception:
        return []
    out = []
    for line in reversed(lines):
        if " HTTP/" in line and " :: " in line:
            out.append(line.strip())
        if len(out) >= limit:
            break
    return out


class DashboardServer(socketserver.ThreadingTCPServer):
    """allow_reuse_address is read during bind(): it must be a class attribute,
    otherwise a restart hits EADDRINUSE while the old port is in TIME_WAIT."""
    allow_reuse_address = True
    daemon_threads = True


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ws-dashboard/1.0"

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, indent=2, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _file(self, path: pathlib.Path, ctype: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"error": "not found", "path": str(path.name)}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/health":
            self._json({"status": "ok", "service": "ws-dashboard", "pid": os.getpid()})
            return
        if path == "/api/status":
            state = None
            try:
                state = (WS_ROOT / "runtime" / "run" / "public-check.state").read_text().strip()
            except Exception:
                state = "unknown"
            try:
                payload = {
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "uptime_s": round(time.time() - STARTED),
                    "public": {
                        "url": CFG.get("public_url"),
                        "state": state,
                        "refresh_seconds": CFG.get("refresh_seconds", 5),
                    },
                    "services": collect_services(),
                    "routes": collect_routes(),
                    "watchers": collect_watchers(),
                    "system": collect_system(),
                    "recent_requests": collect_requests(),
                }
            except Exception as exc:  # never answer with a dropped connection
                self._json({"error": "status collection failed: %s" % exc,
                            "manifest": str(sm.path(WS_ROOT))}, 500)
                return
            self._json(payload)
            return
        if path.startswith("/api/files/"):
            self._files(path)
            return
        if path.startswith("/assets/"):
            name = pathlib.Path(path).name
            ctype = "text/css; charset=utf-8" if name.endswith(".css") else \
                    "application/javascript; charset=utf-8" if name.endswith(".js") else "application/octet-stream"
            self._file(STATIC / name, ctype)
            return
        if path in ("/", "/index.html"):
            self._file(STATIC / "index.html", "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/api/"):
            self._json({"error": "not found", "path": path}, 404)
            return
        body = ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>404</title></head>"
                "<body style='background:#0b0f14;color:#e6edf3;font:16px ui-sans-serif;padding:3rem'>"
                "<h1 style='font-size:1.2rem'>404 — %s</h1>"
                "<p style='color:#8b98a5'>ws-dashboard serves <code>/</code> and <code>/api/*</code>. "
                "Other projects live on their own routes.</p>"
                "<p><a style='color:#79c0ff' href='/'>← dashboard</a> &nbsp; <a style='color:#79c0ff' href='/projects/hello/'>/projects/hello</a></p>"
                "</body></html>" % path).encode()
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _token(self) -> str:
        """Admin token from the request header (preferred: it never lands in a URL/log)."""
        supplied = self.headers.get("X-WS-Files-Token") or ""
        if not supplied:
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                supplied = auth[7:]
        return supplied.strip()

    def _query(self) -> dict:
        """Query string as a dict; no `+`-as-space rewriting, so filenames survive."""
        out = {}
        for part in urllib.parse.urlsplit(self.path).query.split("&"):
            if not part:
                continue
            key, _, value = part.partition("=")
            out[urllib.parse.unquote(key)] = urllib.parse.unquote(value)
        return out

    def _files(self, path: str) -> None:
        supplied = self._token()
        if supplied and not BROWSER.check_token(supplied):
            left = BROWSER.note_failure(self.client_address[0])
            self._json({"error": "invalid token", "attempts_left": left}, 401)
            return
        if supplied:
            BROWSER.note_success(self.client_address[0])
        scope = "admin" if supplied else "root"
        query = self._query()
        rel = query.get("path", "")
        if path == "/api/files/access":
            self._json({**BROWSER.describe(scope), "authenticated": scope == "admin"})
            return
        if path == "/api/files/list":
            payload, error, status = BROWSER.listing(rel, scope)
        elif path == "/api/files/read":
            limit = query.get("max")
            payload, error, status = BROWSER.preview(rel, scope, int(limit) if limit and limit.isdigit() else None)
        elif path == "/api/files/raw":
            body, ctype, error, status = BROWSER.raw(rel, scope)
            if error:
                self._json({"error": error, "path": rel, "scope": scope}, status)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        else:
            self._json({"error": "not found", "path": path}, 404)
            return
        if error:
            self._json({"error": error, "path": rel, "scope": scope}, status)
            return
        self._json(payload)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path != "/api/files/unlock":
            self._json({"error": "read-only API; only /api/files/unlock accepts POST", "path": path}, 405)
            return
        client = self.client_address[0]
        wait = BROWSER.locked(client)
        if wait > 0:
            self._json({"error": f"too many attempts; try again in {int(wait) + 1}s", "locked_for": int(wait) + 1}, 429)
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            body = json.loads(self.rfile.read(length) or b"{}")
            token = str(body.get("token") or "")
        except (TypeError, ValueError):
            self._json({"error": "expected JSON body {\"token\": \"...\"}"}, 400)
            return
        if not BROWSER.check_token(token):
            left = BROWSER.note_failure(client)
            self._json({"error": "invalid token", "attempts_left": left}, 401)
            return
        BROWSER.note_success(client)
        self._json({"ok": True, **BROWSER.describe("admin"), "authenticated": True})

    do_HEAD = do_GET

    def log_message(self, fmt, *args):  # access log stays short
        print("%s %s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), self.client_address[0], fmt % args), flush=True)


def probe_health(port: int) -> int:
    ok, _ = probe(port, "/api/health")
    return 0 if ok else 1


def main() -> int:
    ports_ = sm.ports(ENTRY)
    host, port = CFG.get("listen_host", "127.0.0.1"), ports_[0]
    token, source = BROWSER.ensure_token()
    print("file browser: admin token source=%s scopes: %s (read-only) | print it with: %s --files-token" % (
        source, BROWSER.rel_root + " -> " + BROWSER.rel_admin_root, pathlib.Path(__file__).name), flush=True)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    httpd = DashboardServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.allow_reuse_address = True
    print("%s ws-dashboard up: pid=%d http://%s:%d (public: %s)" % (
        time.strftime("%Y-%m-%dT%H:%M:%S%z"), os.getpid(), host, port, CFG.get("public_url")), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--healthz":
        sys.exit(probe_health(int(sys.argv[2])))
    if len(sys.argv) >= 2 and sys.argv[1] == "--check":
        print("service:  %s (from %s)" % (SERVICE, sm.path(WS_ROOT)))
        print("script:   %s" % ENTRY.get("script"))
        print("ports:    %s" % " ".join(str(p) for p in sm.ports(ENTRY)))
        print("health:   %s" % ENTRY.get("health"))
        print("settings: %s" % json.dumps(CFG, sort_keys=True))
        token, source = BROWSER.ensure_token()
        print("files:    root=%s admin_root=%s token_source=%s" % (
            BROWSER.rel_root, BROWSER.rel_admin_root, source))
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "--files-token":
        token, source = BROWSER.rotate_token() if "--rotate" in sys.argv else BROWSER.ensure_token()
        print(token)
        print("# source: %s" % source, file=sys.stderr)
        sys.exit(0)
    sys.exit(main())

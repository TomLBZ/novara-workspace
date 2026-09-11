#!/usr/bin/env python3
"""ws-gateway - the workspace's persistent L7 path router.

One public entry point (Nginx Proxy Manager -> this router) fans out to any number
of local services by path prefix.  Everything is stdlib-only so it runs on whatever
python the workspace carries, and the route table lives in routes.json next to this
file (see routes.example.json for the schema in use).

    python3 services/gateway/gateway.py            # foreground
    bin/ws-gateway start|stop|status|ensure        # managed

Reserved path: /healthz (never proxied) -> JSON status.
"""
from __future__ import annotations

import http.client
import http.server
import json
import mimetypes
import os
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
WS_ROOT = HERE.parents[1]
ROUTES_FILE = HERE / "routes.json"
PID_FILE = Path(os.environ.get("WS_PID_FILE", str(WS_ROOT / "runtime" / "run" / "gateway.pid")))

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
STARTED = time.time()


def log(msg: str) -> None:
    print("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), msg), flush=True)


def load_routes() -> dict:
    if not ROUTES_FILE.exists():
        raise SystemExit("ws-gateway: missing %s" % ROUTES_FILE)
    cfg = json.loads(ROUTES_FILE.read_text())
    listen = cfg.get("listen") or []
    if not listen:
        raise SystemExit("ws-gateway: routes.json needs a non-empty 'listen' list")
    for r in cfg.get("routes", []):
        if r.get("type") not in ("static", "proxy"):
            raise SystemExit("ws-gateway: route %r needs type static|proxy" % r.get("prefix"))
        if r["type"] == "static" and not r.get("root"):
            raise SystemExit("ws-gateway: static route %r needs 'root'" % r.get("prefix"))
        if r["type"] == "proxy" and not r.get("upstream"):
            raise SystemExit("ws-gateway: proxy route %r needs 'upstream'" % r.get("prefix"))
    return cfg


def pick_route(routes: list, path: str):
    best, best_len = None, -1
    for r in routes:
        prefix = r.get("prefix", "/")
        if prefix == "/" or path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            if len(prefix) > best_len:
                best, best_len = r, len(prefix)
    return best


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ws-gateway/1.0"
    routes: list = []

    # -- helpers ---------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _health(self) -> None:
        body = json.dumps({
            "status": "ok",
            "service": "ws-gateway",
            "pid": os.getpid(),
            "uptime_s": round(time.time() - STARTED),
            "hostname": socket.gethostname(),
            "listen": self.server.listen_ports,
            "routes": [r.get("prefix") for r in self.routes],
        }, indent=2).encode()
        self._send(200, body, "application/json")

    def _static(self, route: dict, path: str) -> None:
        root = (WS_ROOT / route["root"]).resolve()
        rel = path[len(route.get("prefix", "/")):].lstrip("/") if route.get("prefix", "/") != "/" else path.lstrip("/")
        target = (root / rel).resolve()
        if root != target and root not in target.parents:
            self._send(403, b"forbidden\n")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._send(404, b"404 - no such file in %s\n" % route["root"].encode())
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        data = target.read_bytes()
        self._send(200, data, ctype, {"Cache-Control": "no-store"})

    def _proxy(self, route: dict, path: str, query: str) -> None:
        up = urllib.parse.urlsplit(route["upstream"])
        prefix = route.get("prefix", "/")
        rest = path[len(prefix):] if route.get("strip_prefix") else path
        if not rest.startswith("/"):
            rest = "/" + rest
        target = rest + (("?" + query) if query else "")

        headers = {}
        for k, v in self.headers.items():
            if k.lower() in HOP_BY_HOP or k.lower() == "host":
                continue
            headers[k] = v
        headers["Host"] = up.netloc
        headers["X-Forwarded-Host"] = self.headers.get("Host", "")
        headers["X-Forwarded-Prefix"] = prefix
        headers["X-Forwarded-Proto"] = self.headers.get("X-Forwarded-Proto", "http")
        prior = self.headers.get("X-Forwarded-For")
        headers["X-Forwarded-For"] = (prior + ", " if prior else "") + self.client_address[0]

        body = None
        length = self.headers.get("Content-Length")
        if length:
            body = self.rfile.read(int(length))
            headers["Content-Length"] = str(len(body))

        try:
            conn = http.client.HTTPConnection(up.hostname, up.port or 80, timeout=120)
            conn.request(self.command, target, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:  # upstream down / DNS / timeout
            log("proxy-error %s -> %s : %s" % (target, route["upstream"], exc))
            self._send(502, b"502 - upstream %s unreachable: %s\n" % (route["upstream"].encode(), str(exc).encode()))
            return

        payload = resp.read()
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        conn.close()
        self._log_status = resp.status

    # -- dispatch --------------------------------------------------------------
    def _handle(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        route = pick_route(self.routes, path)
        if path == "/healthz":
            self._health()
            return
        if route is None:
            self._send(404, b"404 - ws-gateway has no route for %s\n" % path.encode())
            return
        if route["type"] == "static":
            self._static(route, path)
        else:
            self._proxy(route, path, parsed.query)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _handle

    def log_message(self, fmt, *args):  # noqa: D102
        log("%s %s %s" % (self.client_address[0], self.command, self.path) + " :: " + (fmt % args))


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    listen_ports: list = []


def probe(port: int) -> int:
    """--healthz PORT: exit 0 when the router answers /healthz on that port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
            s.sendall(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            chunks = []
            while True:                      # a single recv() can split headers from body
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks).decode("utf-8", "replace")
    except OSError:
        return 1
    return 0 if '"status": "ok"' in data else 1


def main() -> int:
    cfg = load_routes()
    Handler.routes = cfg["routes"]
    servers, bound = [], []
    for port in cfg["listen"]:
        try:
            srv = Server(("0.0.0.0", int(port)), Handler)
        except OSError as exc:
            log("bind failed on :%d (%s) - continuing" % (port, exc))
            continue
        srv.listen_ports = [int(p) for p in cfg["listen"]]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        bound.append(int(port))
    if not servers:
        log("no listen port could be bound - exiting")
        return 2

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    log("ws-gateway up: pid=%d ports=%s routes=%s" % (
        os.getpid(), bound, [r.get("prefix") for r in Handler.routes]))

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    for srv in servers:
        srv.shutdown()
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
    log("ws-gateway stopped")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--healthz":
        sys.exit(probe(int(sys.argv[2])))
    sys.exit(main())

#!/usr/bin/env python3
"""ws-gateway - the workspace's persistent L7 path router.

One public entry point (Nginx Proxy Manager -> this router) fans out to any number
of local services by path prefix.  Everything is stdlib-only so it runs on whatever
python the workspace carries, and the route table comes from the service manifest
(`services/services.json` -> the `gateway` entry; see `services/services.example.json`
for the tracked baseline and `tools/servicemanifest.py` for the reader/validator).

    python3 services/gateway/gateway.py            # foreground
    bin/ws-gateway start|stop|status|ensure        # managed

Reserved path: /healthz (never proxied) -> JSON status.

Transport notes, because they decide what a service behind this router can do:

* **Responses stream.**  A proxied body is copied to the client as it arrives, with the
  upstream's framing preserved: `Content-Length` is relayed as-is, a chunked upstream is
  re-framed as chunked, and an upstream that says nothing about length gets
  `Connection: close`.  Long-lived streams (SSE, downloads) therefore work.
* **Request bodies stream too**, including `Transfer-Encoding: chunked` uploads.
* **WebSocket upgrades tunnel.**  A proxy route opts in with `"websocket": true`; the
  handshake is validated (RFC 6455: `Sec-WebSocket-Version: 13` plus a 16-byte
  `Sec-WebSocket-Key`), relayed verbatim, and then both directions are spliced
  byte-for-byte until one side is done or the tunnel is idle for `WS_IDLE_TIMEOUT`.
  Upgrades are refused (400) on every route that did not opt in, so the router can never
  be used as an open TCP relay.  Anything a client sends before the 101 is discarded -
  RFC 6455 §4.1 requires the client to wait for the handshake response.
* **A route can forbid rewriting.**  `"no_transform": true` makes the router guarantee
  `Cache-Control: no-transform` on that route's responses, so an intermediate CDN (Cloudflare's
  Rocket Loader) passes the HTML through untouched.  Apps that ship a nonce CSP need it: the
  injected loader's inline activation script carries no nonce, so `script-src … 'nonce-…'`
  blocks it and the page executes nothing (`/vscode` = code-server behaves exactly like this).
  Apps whose HTML we own opt out per script with `data-cfasync="false"` instead.
"""
from __future__ import annotations

import base64
import binascii
import http.client
import http.server
import json
import mimetypes
import os
import select
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
sys.path.insert(0, str(WS_ROOT / "tools"))
import servicemanifest as sm  # noqa: E402  (workspace tool, stdlib only)

PID_FILE = Path(os.environ.get("WS_PID_FILE", str(WS_ROOT / "runtime" / "run" / "gateway.pid")))

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
STARTED = time.time()

# How long the router waits on an upstream (connect, head, one body read).
UPSTREAM_TIMEOUT = 120.0
# A websocket tunnel with no traffic in either direction for this long is closed.
WS_IDLE_TIMEOUT = 900.0
# A non-101 answer to an upgrade: relay it, then keep draining this long at most.
UPGRADE_REFUSED_DRAIN = 30.0
STREAM_CHUNK = 65536
HEAD_LIMIT = 65536
WS_KEY_BYTES = 16

_tunnels = {"open": 0, "total": 0}
_tunnels_lock = threading.Lock()


def log(msg: str) -> None:
    print("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), msg), flush=True)


def token_in(value: str, token: str) -> bool:
    """True when the comma-separated header `value` carries `token` (case-insensitive)."""
    return token.lower() in (t.strip().lower() for t in (value or "").split(","))


def ws_key_ok(key: str) -> bool:
    """True for a valid `Sec-WebSocket-Key`: base64 of exactly 16 bytes."""
    key = (key or "").strip()
    if not key:
        return False
    try:
        raw = base64.b64decode(key + "=" * (-len(key) % 4), validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(raw) == WS_KEY_BYTES


def read_head(sock: socket.socket) -> bytes:
    """Read an HTTP head (status line + headers, body bytes included if they came along)."""
    data = bytearray()
    while b"\r\n\r\n" not in data:
        if len(data) > HEAD_LIMIT:
            raise OSError("upstream head exceeded %d bytes" % HEAD_LIMIT)
        piece = sock.recv(STREAM_CHUNK)
        if not piece:
            break
        data.extend(piece)
    return bytes(data)


def splice(client: socket.socket, upstream: socket.socket, idle: float = WS_IDLE_TIMEOUT) -> tuple[int, int]:
    """Relay bytes both ways until both directions end or nothing moves for `idle` seconds.

    A half close is honoured: an EOF from one side shuts down only the other side's write
    direction, so the remaining direction keeps flowing.  Returns (to_upstream, to_client).
    """
    for sock in (client, upstream):
        sock.settimeout(None)
    reading = [client, upstream]
    moved = {client: 0, upstream: 0}
    try:
        while reading:
            ready, _, _ = select.select(reading, [], [], idle)
            if not ready:
                log("ws-idle: no traffic for %.0fs, closing tunnel" % idle)
                break
            for src in ready:
                dst = upstream if src is client else client
                try:
                    piece = src.recv(STREAM_CHUNK)
                except OSError:
                    piece = b""
                if not piece:
                    reading.remove(src)
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                try:
                    dst.sendall(piece)
                except OSError:
                    reading.clear()
                    break
                moved[src] += len(piece)
    finally:
        for sock in (client, upstream):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    return moved[client], moved[upstream]


def load_routes() -> dict:
    """`{listen: [ports], routes: [...]}` from the gateway entry of the service manifest."""
    try:
        manifest = sm.load(WS_ROOT)
        entry = sm.entry(manifest, os.environ.get("WS_SERVICE", "gateway"))
        routes = sm.routes(manifest, os.environ.get("WS_SERVICE", "gateway"))
        found = sm.problems(manifest)
    except sm.ManifestError as exc:
        raise SystemExit("ws-gateway: %s" % exc)
    if found:
        raise SystemExit("ws-gateway: services/services.json is invalid:\n  - " + "\n  - ".join(found))
    listen = sm.ports(entry)
    if not listen:
        raise SystemExit("ws-gateway: the gateway entry needs 'listen' (list of ports)")
    for r in routes:
        if r.get("type") not in ("static", "proxy"):
            raise SystemExit("ws-gateway: route %r needs type static|proxy" % r.get("prefix"))
        if r["type"] == "static" and not r.get("root"):
            raise SystemExit("ws-gateway: static route %r needs 'root'" % r.get("prefix"))
        if r["type"] == "proxy" and not r.get("upstream"):
            raise SystemExit("ws-gateway: proxy route %r needs 'service' or 'upstream'" % r.get("prefix"))
    return {"listen": listen, "routes": routes}


def pick_route(routes: list, path: str):
    best, best_len = None, -1
    for r in routes:
        prefix = r.get("prefix", "/")
        if prefix == "/" or path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            if len(prefix) > best_len:
                best, best_len = r, len(prefix)
    return best


def with_no_transform(headers: list) -> list:
    """Ensure the outgoing `Cache-Control` says `no-transform` (a route with that flag).

    Why it exists: an intermediate CDN may rewrite an HTML response on the way out.  Cloudflare's
    Rocket Loader does - it rewrites every `<script>` type and injects its own loader - and that
    breaks any app that ships a **nonce CSP**: the injected loader's inline activation script has no
    nonce, so `script-src 'self' … 'nonce-…'` blocks it and the page executes nothing (code-server's
    workbench renders a blank page exactly this way).  `no-transform` is the origin-side instruction
    that tells such a middlebox to pass the response through untouched, so the app's own CSP stays
    intact; apps whose HTML we can edit opt out per script with `data-cfasync="false"` instead
    (`services/sites/hello`, the dashboard).
    """
    out, found = [], False
    for k, v in headers:
        if k.lower() == "cache-control":
            found = True
            if "no-transform" not in v.lower():
                v = ("%s, no-transform" % v) if v.strip() else "no-transform"
        out.append((k, v))
    if not found:
        out.append(("Cache-Control", "no-transform"))
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ws-gateway/1.1"
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
        with _tunnels_lock:
            tunnels = dict(_tunnels)
        body = json.dumps({
            "status": "ok",
            "service": "ws-gateway",
            "pid": os.getpid(),
            "uptime_s": round(time.time() - STARTED),
            "hostname": socket.gethostname(),
            "listen": self.server.listen_ports,
            "routes": [r.get("prefix") for r in self.routes],
            "websockets": tunnels,
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

    # -- request side ----------------------------------------------------------
    def _target_of(self, route: dict) -> str:
        """The path (+query) to ask the upstream for, honouring `strip_prefix`."""
        parsed = urllib.parse.urlsplit(self.path)
        prefix = route.get("prefix", "/")
        rest = parsed.path[len(prefix):] if route.get("strip_prefix") else parsed.path
        if not rest.startswith("/"):
            rest = "/" + rest
        return rest + (("?" + parsed.query) if parsed.query else "")

    def _forwarded_headers(self, route: dict, upstream_netloc: str) -> dict:
        headers = {}
        for k, v in self.headers.items():
            low = k.lower()
            if low in HOP_BY_HOP or low == "host":
                continue
            headers[k] = v
        headers["Host"] = upstream_netloc
        headers["X-Forwarded-Host"] = self.headers.get("Host", "")
        headers["X-Forwarded-Prefix"] = route.get("prefix", "/")
        headers["X-Forwarded-Proto"] = self.headers.get("X-Forwarded-Proto", "http")
        prior = self.headers.get("X-Forwarded-For")
        headers["X-Forwarded-For"] = (prior + ", " if prior else "") + self.client_address[0]
        return headers

    def _request_body(self, chunked: bool):
        """An iterator over the client's body, or None.  Both framing kinds stream."""
        if chunked:
            return self._chunked_body()
        raw = self.headers.get("Content-Length")
        if not raw:
            return None
        try:
            length = int(raw)
        except ValueError:
            return None
        return self._sized_body(length)

    def _sized_body(self, length: int):
        left = length
        while left > 0:
            piece = self.rfile.read1(min(STREAM_CHUNK, left))
            if not piece:
                raise ConnectionError("client sent %d of %d body bytes" % (length - left, length))
            left -= len(piece)
            yield piece

    def _chunked_body(self):
        while True:
            line = self.rfile.readline(HEAD_LIMIT)
            if not line:
                raise ConnectionError("client stopped inside a chunked body")
            try:
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
            except ValueError:
                raise ConnectionError("client sent a bad chunk size: %r" % line[:32])
            if size == 0:
                while True:                        # trailers (rare) up to the blank line
                    trailer = self.rfile.readline(HEAD_LIMIT)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        return
            left = size
            while left:
                piece = self.rfile.read1(min(STREAM_CHUNK, left))
                if not piece:
                    raise ConnectionError("client stopped inside a chunk")
                left -= len(piece)
                yield piece
            self.rfile.read(2)                     # CRLF that closes the chunk

    # -- proxying --------------------------------------------------------------
    def _proxy(self, route: dict) -> None:
        up = urllib.parse.urlsplit(route["upstream"])
        target = self._target_of(route)
        headers = self._forwarded_headers(route, up.netloc)
        chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()

        conn = None
        try:
            body = self._request_body(chunked)
            conn = http.client.HTTPConnection(up.hostname, up.port or 80, timeout=UPSTREAM_TIMEOUT)
            conn.putrequest(self.command, target, skip_host=True, skip_accept_encoding=True)
            for k, v in headers.items():
                conn.putheader(k, v)
            if chunked:
                conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            if body is not None:
                for piece in body:
                    conn.send(b"%x\r\n%s\r\n" % (len(piece), piece) if chunked else piece)
                if chunked:
                    conn.send(b"0\r\n\r\n")
            resp = conn.getresponse()
        except Exception as exc:  # upstream down / bad request / client vanished mid-body
            if conn is not None:
                conn.close()
            log("proxy-error %s -> %s : %s" % (target, route["upstream"], exc))
            self._send(502, b"502 - upstream %s unreachable: %s\n"
                       % (route["upstream"].encode(), str(exc).encode()))
            return

        try:
            self._relay(resp, route)
        finally:
            conn.close()
        self._log_status = resp.status

    def _relay(self, resp: http.client.HTTPResponse, route: dict | None = None) -> None:
        """Stream one upstream response back, keeping its framing semantics."""
        passthrough, upstream_length = [], None
        for k, v in resp.getheaders():
            low = k.lower()
            if low == "content-length":
                upstream_length = v          # described by whatever framing we choose
                continue
            if low in HOP_BY_HOP:
                continue
            passthrough.append((k, v))
        if route and route.get("no_transform"):
            passthrough = with_no_transform(passthrough)

        has_body = self.command != "HEAD" and resp.status >= 200 and resp.status not in (204, 304)
        if not has_body:
            framing = "none"
        elif resp.chunked:
            framing = "chunked"
        elif resp.length is not None:
            framing = "length"
        else:
            framing = "close"

        self.send_response(resp.status, resp.reason)
        for k, v in passthrough:
            self.send_header(k, v)
        if framing == "chunked":
            self.send_header("Transfer-Encoding", "chunked")
        elif framing == "length":
            self.send_header("Content-Length", str(resp.length))
        elif framing == "close":
            self.close_connection = True
            self.send_header("Connection", "close")
        elif upstream_length is not None:
            self.send_header("Content-Length", upstream_length)   # a HEAD describes its body
        self.end_headers()

        if framing == "none":
            return
        limit = resp.length if framing == "length" else None
        sent = 0
        while True:
            piece = resp.read1(STREAM_CHUNK)
            if not piece:
                break
            if framing == "chunked":
                self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
            else:
                self.wfile.write(piece)
            self.wfile.flush()               # one upstream read = one bite at the client
            sent += len(piece)
            if limit is not None and sent >= limit:
                break
        if limit is not None and sent < limit:
            self.close_connection = True
            log("proxy-short-body upstream=%s sent=%d of %d" % (resp.status, sent, limit))
        if framing == "chunked":
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    # -- websocket upgrades ----------------------------------------------------
    def _upgrade_requested(self) -> bool:
        return (token_in(self.headers.get("Connection", ""), "upgrade")
                and (self.headers.get("Upgrade") or "").strip().lower() == "websocket")

    def _websocket(self, route: dict) -> None:
        """Relay the handshake, then splice the connection both ways."""
        key = self.headers.get("Sec-WebSocket-Key") or ""
        version = (self.headers.get("Sec-WebSocket-Version") or "").strip()
        if version != "13" or not ws_key_ok(key):
            log("ws-refused prefix=%s client=%s version=%r key=%s"
                % (route.get("prefix"), self.client_address[0], version, "bad" if key else "missing"))
            self._send(400, b"400 - not a websocket handshake: RFC 6455 needs "
                            b"Sec-WebSocket-Version: 13 and a 16-byte Sec-WebSocket-Key\n")
            return

        up = urllib.parse.urlsplit(route["upstream"])
        target = self._target_of(route)
        head = ["GET %s HTTP/1.1" % target, "Host: %s" % up.netloc]
        for k, v in self.headers.items():
            low = k.lower()
            if low in ("host", "connection", "upgrade", "x-forwarded-for", "x-forwarded-host",
                       "x-forwarded-prefix", "x-forwarded-proto"):
                continue
            head.append("%s: %s" % (k, v))
        head += ["Connection: Upgrade", "Upgrade: websocket",
                 "X-Forwarded-Prefix: %s" % route.get("prefix", "/"),
                 "X-Forwarded-Proto: %s" % self.headers.get("X-Forwarded-Proto", "http"),
                 "X-Forwarded-For: %s" % self.client_address[0]]
        request_head = ("\r\n".join(head) + "\r\n\r\n").encode("latin-1")

        try:
            upstream = socket.create_connection((up.hostname, up.port or 80), timeout=UPSTREAM_TIMEOUT)
        except OSError as exc:
            log("ws-error %s -> %s : %s" % (target, route["upstream"], exc))
            self._send(502, b"502 - upstream %s unreachable: %s\n"
                       % (route["upstream"].encode(), str(exc).encode()))
            return
        try:
            upstream.sendall(request_head)
            upstream.settimeout(UPSTREAM_TIMEOUT)
            response_head = read_head(upstream)
            try:
                status_code = int(response_head.split(b" ", 2)[1])
            except (IndexError, ValueError):
                status_code = 0
            if status_code != 101:
                log("ws-refused-by-upstream prefix=%s target=%s status=%r"
                    % (route.get("prefix"), target, response_head.split(b"\r\n", 1)[0][:80]))
                self.connection.sendall(response_head)
                self.close_connection = True
                splice(self.connection, upstream, UPGRADE_REFUSED_DRAIN)
                return

            self.connection.sendall(response_head)
            self.close_connection = True            # never reuse this connection for HTTP
            with _tunnels_lock:
                _tunnels["open"] += 1
                _tunnels["total"] += 1
            began = time.time()
            log("ws-open prefix=%s target=%s client=%s" % (route.get("prefix"), target, self.client_address[0]))
            to_up, to_client = splice(self.connection, upstream)
            with _tunnels_lock:
                _tunnels["open"] -= 1
            log("ws-close prefix=%s client=%s seconds=%.1f to_upstream=%d to_client=%d"
                % (route.get("prefix"), self.client_address[0], time.time() - began, to_up, to_client))
        except OSError as exc:
            log("ws-error prefix=%s : %s" % (route.get("prefix"), exc))
        finally:
            upstream.close()

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
        if self._upgrade_requested():
            if route["type"] == "proxy" and route.get("websocket"):
                self._websocket(route)
            else:                                   # never a general-purpose TCP relay
                log("ws-refused prefix=%s client=%s (route did not opt in)"
                    % (route.get("prefix"), self.client_address[0]))
                self._send(400, b"400 - websocket upgrades are not enabled for %s\n" % path.encode())
            return
        if route["type"] == "static":
            self._static(route, path)
        else:
            self._proxy(route)

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
        os.getpid(), bound, [(r.get("prefix"), "ws" if r.get("websocket") else "http") for r in Handler.routes]))

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

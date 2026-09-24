#!/usr/bin/env python3
"""Fixtures shared by the ws-gateway test scripts (stdlib only).

Three pieces, all local to the test process:

* `Upstream` - a real HTTP/1.1 server on a free loopback port that answers the shapes the
  router has to handle: fixed length, chunked streaming, big bodies, POST bodies (both
  framings), HEAD, a websocket echo `/ws`, and an upstream that refuses an upgrade.
* `Gateway`  - runs `services/gateway/gateway.py` against a **scratch manifest** under
  `tmp/` (`WS_MANIFEST` + `WS_PID_FILE`), so the live manifest and the live router are
  never touched.
* `Reader` / frame + response helpers - raw socket reading, so framing (not just the
  decoded body) can be asserted.  A websocket client that masks, per RFC 6455.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import pathlib
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parents[1]
WS_ROOT = HERE.parents[1]
GATEWAY = HERE / "gateway.py"
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
BIG = 4 * 1024 * 1024


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- websocket framing ------------------------------------------------------
def build_frame(payload: bytes, opcode: int = 0x1, mask: bool = False, fin: bool = True) -> bytes:
    head = bytearray([(0x80 if fin else 0x00) | opcode])
    size = len(payload)
    flag = 0x80 if mask else 0x00
    if size < 126:
        head.append(flag | size)
    elif size < 65536:
        head.append(flag | 126)
        head += struct.pack(">H", size)
    else:
        head.append(flag | 127)
        head += struct.pack(">Q", size)
    if mask:
        key = os.urandom(4)
        head += key
        payload = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
    return bytes(head) + payload


class Reader:
    """A socket reader that keeps the bytes it read past the thing it was asked for."""

    def __init__(self, sock: socket.socket, leftover: bytes = b"") -> None:
        self.sock = sock
        self.buf = bytearray(leftover)

    def take(self, size: int) -> bytes:
        while len(self.buf) < size:
            piece = self.sock.recv(65536)
            if not piece:
                raise EOFError("peer closed with %d of %d bytes" % (len(self.buf), size))
            self.buf.extend(piece)
        out = bytes(self.buf[:size])
        del self.buf[:size]
        return out

    def line(self, limit: int = 65536) -> bytes:
        while b"\r\n" not in self.buf:
            if len(self.buf) > limit:
                raise ValueError("no line within %d bytes" % limit)
            piece = self.sock.recv(65536)
            if not piece:
                out, self.buf = bytes(self.buf), bytearray()
                return out
            self.buf.extend(piece)
        end = self.buf.index(b"\r\n") + 2
        out = bytes(self.buf[:end])
        del self.buf[:end]
        return out

    def take_all(self) -> bytes:
        while True:
            piece = self.sock.recv(65536)
            if not piece:
                out, self.buf = bytes(self.buf), bytearray()
                return out
            self.buf.extend(piece)

    def frame(self) -> tuple[bool, int, bytes]:
        first, second = self.take(1)[0], self.take(1)[0]
        size = second & 0x7F
        if size == 126:
            size = struct.unpack(">H", self.take(2))[0]
        elif size == 127:
            size = struct.unpack(">Q", self.take(8))[0]
        key = self.take(4) if second & 0x80 else None
        payload = self.take(size) if size else b""
        if key:
            payload = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
        return bool(first & 0x80), first & 0x0F, payload


def read_response(reader: Reader, until_close: bool = False) -> tuple[int, dict, bytes]:
    """Status, lower-cased headers, body - chunked and fixed-length both decoded."""
    head = reader.line().decode("latin-1")
    if not head:
        raise EOFError("no status line")
    status = int(head.split()[1])
    headers: dict[str, str] = {}
    while True:
        line = reader.line()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    body = b""
    if until_close:
        body = reader.take_all()
    elif headers.get("transfer-encoding", "").lower() == "chunked":
        while True:
            size = int(reader.line().split(b";")[0].strip() or b"0", 16)
            if size == 0:
                reader.line()
                break
            body += reader.take(size)
            reader.take(2)
    elif "content-length" in headers:
        body = reader.take(int(headers["content-length"]))
    return status, headers, body


class Upstream:
    """The service behind the router: real HTTP + a websocket echo."""

    def __init__(self) -> None:
        fixture = self
        handler = type("Handler", (http.server.BaseHTTPRequestHandler,), {
            "protocol_version": "HTTP/1.1",
            "log_message": lambda *a: None,
            "do_GET": lambda self: fixture.get(self),
            "do_HEAD": lambda self: fixture.head(self),
            "do_POST": lambda self: fixture.post(self),
        })
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.paths: list[str] = []

    def __enter__(self) -> "Upstream":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- helpers ---------------------------------------------------------------
    def read_body(self, handler) -> bytes:
        if "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
            out = bytearray()
            while True:                       # read from rfile: it has already buffered some
                size = int(handler.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    handler.rfile.readline()
                    break
                out += handler.rfile.read(size)
                handler.rfile.read(2)
            return bytes(out)
        length = int(handler.headers.get("Content-Length") or 0)
        return handler.rfile.read(length) if length else b""

    def reply(self, handler, status: int, body: bytes, ctype: str = "text/plain", extra: dict | None = None) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)

    # -- routes ----------------------------------------------------------------
    def get(self, handler) -> None:
        path = handler.path.split("?")[0]
        self.paths.append(handler.path)
        if path == "/ws" and (handler.headers.get("Upgrade") or "").lower() == "websocket":
            return self.ws(handler)
        if path == "/text":
            return self.reply(handler, 200, b"hello from upstream\n", extra={
                "X-Upstream-Test": "yes", "Connection": "keep-alive", "Keep-Alive": "timeout=5"})
        if path == "/stream":
            handler.send_response(200)
            handler.send_header("Content-Type", "text/plain")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            for index in range(3):
                piece = ("piece-%d\n" % index).encode()
                handler.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                handler.wfile.flush()
                time.sleep(0.6)
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
            return
        if path == "/big":
            size = int(handler.path.split("n=")[-1]) if "n=" in handler.path else BIG
            body = (b"0123456789abcdef" * ((size // 16) + 1))[:size]
            return self.reply(handler, 200, body, "application/octet-stream")
        if path == "/cached":
            # An upstream that already has a Cache-Control of its own (the router must merge,
            # not replace, when a route asks for no-transform).
            return self.reply(handler, 200, b"from cache-control upstream\n",
                              extra={"Cache-Control": "no-store"})
        if path == "/refuse-upgrade":
            return self.reply(handler, 403, b"upstream says no\n")
        if path == "/no-length":                      # HTTP/1.0 style: body ends at EOF
            handler.send_response(200)
            handler.send_header("Content-Type", "text/plain")
            handler.close_connection = True
            handler.end_headers()
            handler.wfile.write(b"until-eof\n")
            handler.wfile.flush()
            return
        return self.reply(handler, 404, b"nope\n")

    def head(self, handler) -> None:
        self.paths.append(handler.path)
        if handler.path.split("?")[0] == "/text":
            return self.reply(handler, 200, b"hello from upstream\n")
        return self.reply(handler, 404, b"")

    def post(self, handler) -> None:
        self.paths.append(handler.path)
        body = self.read_body(handler)
        payload = json.dumps({"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}).encode()
        return self.reply(handler, 200, payload, "application/json")

    def ws(self, handler) -> None:
        """RFC 6455 handshake + echo.  The gateway must relay this verbatim."""
        key = handler.headers.get("Sec-WebSocket-Key") or ""
        accept = base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
        handler.wfile.write(("HTTP/1.1 101 Switching Protocols\r\n"
                             "Upgrade: websocket\r\n"
                             "Connection: Upgrade\r\n"
                             "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
        handler.wfile.flush()
        reader = Reader(handler.connection)
        try:
            while True:
                _fin, opcode, payload = reader.frame()
                if opcode == 0x8:
                    handler.connection.sendall(build_frame(b"", 0x8))
                    break
                if opcode == 0x9:
                    handler.connection.sendall(build_frame(payload, 0xA))
                    continue
                handler.connection.sendall(build_frame(payload, opcode))
        except (EOFError, OSError):
            pass
        try:
            handler.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class Ws:
    """A websocket client: masked frames, verifies `Sec-WebSocket-Accept`."""

    def __init__(self, sock: socket.socket, reader: Reader, status: int, headers: dict) -> None:
        self.sock, self.reader, self.status, self.headers = sock, reader, status, headers

    @classmethod
    def connect(cls, port: int, path: str, host: str = "127.0.0.1",
                key: str | None = None, version: str = "13", timeout: float = 10.0) -> "Ws":
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        key = key if key is not None else base64.b64encode(os.urandom(16)).decode()
        request = ["GET %s HTTP/1.1" % path, "Host: %s:%d" % (host, port),
                   "Upgrade: websocket", "Connection: Upgrade",
                   "Sec-WebSocket-Version: %s" % version, "Sec-WebSocket-Key: %s" % key]
        sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode())
        reader = Reader(sock)
        status, headers, _body = read_response(reader)
        expected = base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
        cls.last_key, cls.last_expected = key, expected
        return cls(sock, reader, status, headers)

    def send(self, payload: bytes, opcode: int = 0x1) -> None:
        self.sock.sendall(build_frame(payload, opcode, mask=True))

    def recv(self) -> tuple[int, bytes]:
        _fin, opcode, payload = self.reader.frame()
        return opcode, payload

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Gateway:
    """`gateway.py` running on a scratch manifest, on a fresh loopback port.

    `services` adds further manifest entries (a test that routes through a real service
    needs them in the same scratch file, so the live one stays untouched).
    """

    def __init__(self, tmp: pathlib.Path, routes: list[dict], services: dict | None = None,
                 port: int | None = None) -> None:
        self.tmp = tmp
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.port = port or free_port()
        self.manifest = self.tmp / "services-test.json"
        self.log = self.tmp / "gateway-test.log"
        manifest = {
            "_about": "scratch manifest for service tests (never the live one)",
            "gateway": {"script": "services/gateway/gateway.py", "listen": [self.port],
                        "health": "/healthz", "log": str(self.log), "routes": routes},
        }
        manifest.update(services or {})
        self.manifest.write_text(json.dumps(manifest, indent=2))
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> "Gateway":
        env = dict(os.environ, WS_ROOT=str(WS_ROOT), WS_MANIFEST=str(self.manifest),
                   WS_PID_FILE=str(self.tmp / "gateway-test.pid"))
        self.out = self.log.open("wb")
        self.proc = subprocess.Popen([sys.executable, str(GATEWAY)], env=env,
                                     stdout=self.out, stderr=subprocess.STDOUT, cwd=str(WS_ROOT))
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("gateway exited early (rc=%s):\n%s" % (self.proc.returncode, self.tail()))
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % self.port, timeout=1) as r:
                    if json.loads(r.read().decode())["status"] == "ok":
                        return self
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.1)
        raise RuntimeError("gateway did not answer /healthz:\n%s" % self.tail())

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.out.close()

    def tail(self, lines: int = 20) -> str:
        try:
            return "\n".join(self.log.read_text().splitlines()[-lines:])
        except OSError:
            return "(no log)"

    def health(self) -> dict:
        with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % self.port, timeout=5) as r:
            return json.loads(r.read().decode())

    def connect(self, timeout: float = 10.0) -> socket.socket:
        return socket.create_connection(("127.0.0.1", self.port), timeout=timeout)

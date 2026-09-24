#!/usr/bin/env python3
"""End-to-end checks for the `ws-vscode` service (stdlib only).

Replicates the whole path a user takes - public URL -> ws-gateway -> code-server - against
scratch state under `tmp/`, on scratch ports, with a scratch password file: the live
service, its data directory and `services/services.json` are never touched.

  python3 services/vscode/tests/test_service.py

What is pinned down: the login redirect stays *inside* the prefix, the password gates the
workbench, the workbench HTML is served under the prefix, a foreign `Origin` is refused
(code-server 403s it), and the IDE websocket - the one the workbench opens to the extension
host - actually upgrades through the router to 101.

Exit codes: 0 = all checks passed, 1 = a check failed, 3 = code-server is not installed
(tools/bootstrap.sh) - the caller decides whether that is a warning or an error.
"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parents[1]
WS_ROOT = HERE.parents[1]
sys.path.insert(0, str(WS_ROOT / "services" / "gateway" / "tests"))
import fixture  # noqa: E402  (shared harness: ports, reader, response parsing, gateway)

CODE_SERVER = WS_ROOT / "runtime" / "code-server" / "current" / "bin" / "code-server"
PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("PASS %s%s" % (name, (" (%s)" % detail) if detail else ""))
    else:
        FAIL += 1
        print("FAIL %s%s" % (name, (" :: %s" % detail) if detail else ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class VscodeService:
    """`services/vscode/vscode.py` on scratch state, started the way the supervisor does."""

    def __init__(self, tmp: pathlib.Path, manifest: pathlib.Path, port: int, password_file: pathlib.Path) -> None:
        self.tmp, self.manifest, self.port, self.password_file = tmp, manifest, port, password_file
        self.log = tmp / "vscode-test.log"
        self.pid_file = tmp / "vscode-test.pid"
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> "VscodeService":
        env = dict(os.environ, WS_ROOT=str(WS_ROOT), WS_MANIFEST=str(self.manifest),
                   WS_SERVICE="vscode", WS_PID_FILE=str(self.pid_file))
        self.out = self.log.open("wb")
        self.proc = subprocess.Popen([sys.executable, str(HERE / "vscode.py")], env=env,
                                     stdout=self.out, stderr=subprocess.STDOUT, cwd=str(WS_ROOT))
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("the service exited early (rc=%s):\n%s" % (self.proc.returncode, self.tail()))
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % self.port, timeout=1) as resp:
                    if resp.status == 200:
                        return self
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        raise RuntimeError("code-server never answered /healthz:\n%s" % self.tail())

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.out.close()

    def tail(self, lines: int = 25) -> str:
        try:
            return "\n".join(self.log.read_text().splitlines()[-lines:])
        except OSError:
            return "(no log)"

    def password(self) -> str:
        return self.password_file.read_text().strip()

    def alive(self) -> bool:
        return bool(self.proc) and self.proc.poll() is None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A browser would follow a redirect; the test needs to see the 302 and its Location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class Browser:
    """One cookie jar, two modes: `stay()` stops at a redirect, `follow()` follows it."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        processor = urllib.request.HTTPCookieProcessor(self.jar)
        self.stay = urllib.request.build_opener(processor, NoRedirect)
        self.follow = urllib.request.build_opener(processor)

    def get(self, path: str, follow: bool = False, data: bytes | None = None) -> tuple[int, dict, bytes, str]:
        request = urllib.request.Request(self.base + path, data=data,
                                        method="POST" if data is not None else "GET")
        if data is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        opener = self.follow if follow else self.stay
        try:
            with opener.open(request, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read(), resp.url
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read(), exc.url

    def post_password(self, password: str) -> tuple[int, dict, bytes, str]:
        return self.get("/vscode/login", data=urllib.parse.urlencode({"password": password}).encode())

    def cookie_header(self) -> str:
        return "; ".join("%s=%s" % (c.name, c.value) for c in self.jar)

    def cookie_names(self) -> list:
        return [c.name for c in self.jar]


def websocket_upgrade(port: int, path: str, origin: str | None, cookie: str | None) -> tuple[str, int]:
    """(status line, bytes the server sent after a 101) - a plain RFC 6455 handshake."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=20)
    key = base64.b64encode(os.urandom(16)).decode()
    head = ["GET %s HTTP/1.1" % path, "Host: 127.0.0.1:%d" % port, "Upgrade: websocket",
            "Connection: Upgrade", "Sec-WebSocket-Version: 13", "Sec-WebSocket-Key: %s" % key]
    if origin:
        head.append("Origin: %s" % origin)
    if cookie:
        head.append("Cookie: %s" % cookie)
    sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
    data = b""
    while b"\r\n\r\n" not in data:
        piece = sock.recv(4096)
        if not piece:
            break
        data += piece
    line = data.split(b"\r\n", 1)[0].decode()
    after = 0
    if " 101 " in line:
        try:
            sock.settimeout(8)
            after = len(sock.recv(4096))            # the workbench's first frame
        except OSError:
            after = 0
    sock.close()
    return line, after


def main() -> int:
    if not CODE_SERVER.is_file():
        print("SKIP code-server is not installed at %s - run tools/bootstrap.sh" % CODE_SERVER)
        return 3

    tmp = WS_ROOT / "tmp" / "vscode-service-test"
    password_file = tmp / "password"
    runtime_dir = tmp / "runtime"
    for path in (tmp, runtime_dir):
        path.mkdir(parents=True, exist_ok=True)
    if password_file.exists():
        password_file.unlink()

    service_port, gateway_port = free_port(), free_port()
    trusted = "127.0.0.1:%d" % gateway_port
    services = {"vscode": {
        "script": "services/vscode/vscode.py",
        "port": service_port,
        "health": "/healthz",
        "log": str(tmp / "vscode-service.log"),
        "settings": {"folder": ".", "trusted_origins": [trusted], "password_file": str(password_file),
                     "runtime_dir": str(runtime_dir)},
    }}
    routes = [{"prefix": "/vscode", "type": "proxy", "service": "vscode", "strip_prefix": True,
               "websocket": True}]
    gateway = fixture.Gateway(tmp, routes, services=services, port=gateway_port)

    with VscodeService(tmp, gateway.manifest, service_port, password_file) as service, gateway:
        base = "http://127.0.0.1:%d" % gateway.port
        password = service.password()
        check("the service generated a password file (0600)",
              bool(password) and oct(password_file.stat().st_mode)[-3:] == "600",
              "%d chars, mode %s" % (len(password), oct(password_file.stat().st_mode)[-3:]))

        browser = Browser(base)
        status, headers, _body, _url = browser.get("/vscode/")
        location = headers.get("Location", "")
        check("an unauthenticated visit is redirected to the login page inside the prefix",
              status == 302 and location.endswith("login") and not location.startswith("/"),
              "status=%s location=%r" % (status, location))

        status, headers, body, _url = browser.get("/vscode/login")
        check("the login page renders under the prefix",
              status == 200 and b'name="password"' in body and b"code-server" in body,
              "status=%s bytes=%d" % (status, len(body)))

        status, headers, body, _url = browser.get("/vscode/healthz")
        check("the service's own health endpoint answers through the router",
              status == 200 and json.loads(body.decode()).get("status") in ("alive", "expired"),
              "status=%s body=%r" % (status, body[:60]))

        status, _headers, _body, _url = browser.post_password("wrong-password")
        check("a wrong password does not get a session",
              status == 200 and not browser.cookie_names(),
              "status=%s cookies=%s" % (status, browser.cookie_names()))

        status, headers, _body, _url = browser.post_password(password)
        check("the real password grants a session",
              status == 302 and any("session" in name for name in browser.cookie_names()),
              "status=%s cookies=%s" % (status, browser.cookie_names()))

        status, headers, body, final = browser.get("/vscode/", follow=True)
        page = body.decode("utf-8", "replace")
        socket_path = re.search(r"stable-[a-f0-9]{20,}", page)
        check("the workbench page is served under the prefix once authenticated",
              status == 200 and bool(socket_path) and "workbench.js" in page,
              "status=%s final=%s socket=%s" % (status, final, socket_path.group(0) if socket_path else None))
        check("the socket path stays inside the prefix",
              bool(socket_path) and final.startswith(base + "/vscode/"), final)

        cookie_header = browser.cookie_header()
        ws_path = "/vscode/%s?reconnectionToken=00000000-0000-0000-0000-000000000000&reconnection=false&skipWebSocketFrames=false" % (
            socket_path.group(0) if socket_path else "stable-0000")
        line, _after = websocket_upgrade(gateway.port, ws_path, origin="http://evil.example", cookie=cookie_header)
        check("a foreign Origin is refused before the upgrade", " 403 " in line, line)

        line, after = websocket_upgrade(gateway.port, ws_path, origin="http://" + trusted, cookie=cookie_header)
        check("the IDE websocket upgrades through the router to 101 and the server speaks first",
              " 101 " in line and after > 0, "%s then %d bytes" % (line, after))

        line, _after = websocket_upgrade(gateway.port, ws_path, origin="http://" + trusted, cookie=None)
        check("without a session the socket is refused", " 401 " in line, line)

        log = gateway.tail(400)
        # Only the open is asserted here: code-server notices a vanished peer on its next
        # websocket keepalive (measured: ~30s), so waiting for the close would dominate the
        # suite.  The close path - including the byte counts - is pinned down by
        # services/gateway/tests/test_websocket.py, whose upstream closes promptly.
        check("the router audited the tunnel it opened",
              log.count("ws-open prefix=/vscode") == 1, "open=%d" % log.count("ws-open prefix=/vscode"))

        check("the wrapper is still the live process", service.alive())

    running = [line for line in subprocess.run(["pgrep", "-af", "code-server"],
                                               capture_output=True, text=True).stdout.splitlines()
               if str(runtime_dir) in line]
    check("stopping the service took its code-server with it", not running,
          running[0][:120] if running else "none left")

    print("\nVSCODE SERVICE: %d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

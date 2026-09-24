#!/usr/bin/env python3
"""Websocket checks for ws-gateway - pure units, manifest validation, live tunnels.

  python3 services/gateway/tests/test_websocket.py

Live tunnels go through a running `gateway.py` (scratch manifest, scratch port) to a real
upstream that speaks RFC 6455, so what is asserted is the bytes on the wire: the 101 and its
`Sec-WebSocket-Accept`, both directions of the splice, control frames, 64-bit lengths, half
close, and the three ways an upgrade must be refused.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import pathlib
import socket
import sys
import threading
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fixture  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parents[1]
PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("PASS %s%s" % (name, (" (%s)" % detail) if detail else ""))
    else:
        FAIL += 1
        print("FAIL %s%s" % (name, (" :: %s" % detail) if detail else ""))


def load_gateway():
    spec = importlib.util.spec_from_file_location("ws_gateway_under_test", HERE / "gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unit_tests(gw) -> None:
    good_key = base64.b64encode(os.urandom(16)).decode()
    check("ws_key_ok accepts a 16-byte base64 key", gw.ws_key_ok(good_key), good_key)
    check("ws_key_ok accepts a key sent without padding",
          gw.ws_key_ok(good_key.rstrip("=")), good_key.rstrip("="))
    check("ws_key_ok rejects a short key", not gw.ws_key_ok(base64.b64encode(b"short").decode()))
    check("ws_key_ok rejects junk", not gw.ws_key_ok("not-base64!!") and not gw.ws_key_ok(""))

    check("token_in finds a token in a list", gw.token_in("keep-alive, Upgrade", "upgrade"))
    check("token_in is case-insensitive both ways", gw.token_in("Upgrade", "upgrade"))
    check("token_in does not match a substring", not gw.token_in("no-upgrade-here", "upgrade"))

    routes = [{"prefix": "/up", "type": "proxy"}, {"prefix": "/", "type": "proxy"},
              {"prefix": "/upside", "type": "proxy"}]
    picked = lambda p: (gw.pick_route(routes, p) or {}).get("prefix")
    check("pick_route takes the longest matching prefix",
          picked("/up/ws") == "/up" and picked("/upside/down") == "/upside" and picked("/else") == "/",
          "%s %s %s" % (picked("/up/ws"), picked("/upside/down"), picked("/else")))
    check("pick_route does not match a prefix without a path boundary", picked("/upper") == "/")

    # splice(): idle tunnel, then a real two-way relay with a half close.
    a, b = socket.socketpair()
    c, d = socket.socketpair()
    began = time.time()
    moved = gw.splice(a, c, idle=0.4)
    idle_for = time.time() - began
    check("splice() closes an idle tunnel instead of hanging",
          0.35 <= idle_for < 2.0 and moved == (0, 0), "%.2fs" % idle_for)
    a.close(); c.close()

    a, b = socket.socketpair()
    c, d = socket.socketpair()
    thread = threading.Thread(target=lambda: gw.splice(a, c, idle=5.0), daemon=True)
    thread.start()
    d.sendall(b"from-upstream")
    check("splice() relays upstream -> client", b.recv(64) == b"from-upstream")
    b.sendall(b"from-client")
    check("splice() relays client -> upstream", d.recv(64) == b"from-client")
    b.shutdown(socket.SHUT_WR)
    check("splice() propagates a half close", d.recv(64) == b"")
    d.shutdown(socket.SHUT_WR)
    thread.join(timeout=3)
    check("splice() ends once both directions are done", not thread.is_alive())
    for sock in (a, b, c, d):
        sock.close()


def manifest_tests() -> None:
    sys.path.insert(0, str(fixture.WS_ROOT / "tools"))
    import servicemanifest as sm

    def problems(routes):
        return sm.problems({"gw": {"script": "s", "health": "/h", "log": "l", "listen": [1], "routes": routes}})

    base = {"prefix": "/x", "type": "proxy", "upstream": "http://127.0.0.1:1"}
    check("the manifest accepts websocket: true on a proxy route",
          not problems([dict(base, websocket=True)]))
    check("the manifest accepts websocket: false on a proxy route",
          not problems([dict(base, websocket=False)]))
    check("the manifest rejects a non-boolean websocket flag",
          any("websocket" in p for p in problems([dict(base, websocket="yes")])),
          "; ".join(problems([dict(base, websocket="yes")])))
    check("the manifest rejects websocket on a static route",
          any("websocket" in p for p in problems([{"prefix": "/x", "type": "static", "root": "services/sites/hello",
                                                   "websocket": True}])),
          "; ".join(problems([{"prefix": "/x", "type": "static", "root": "r", "websocket": True}])))


def live_tests(tmp: pathlib.Path) -> None:
    with fixture.Upstream() as up:
        routes = [
            {"prefix": "/up", "type": "proxy", "upstream": "http://127.0.0.1:%d" % up.port,
             "strip_prefix": True, "websocket": True},
            {"prefix": "/plain", "type": "proxy", "upstream": "http://127.0.0.1:%d" % up.port},
        ]
        with fixture.Gateway(tmp, routes) as gw:
            ws = fixture.Ws.connect(gw.port, "/up/ws")
            check("the router relays the 101 with the upstream's own accept key",
                  ws.status == 101 and ws.headers.get("sec-websocket-accept") == fixture.Ws.last_expected
                  and ws.headers.get("upgrade", "").lower() == "websocket",
                  "status=%s accept=%s" % (ws.status, ws.headers.get("sec-websocket-accept")))

            ws.send(b"hello websocket")
            opcode, payload = ws.recv()
            check("text frames echo through the tunnel", (opcode, payload) == (0x1, b"hello websocket"),
                  "opcode=%s %r" % (opcode, payload))

            blob = bytes(range(256)) * 400                       # 100 KiB binary
            ws.send(blob, opcode=0x2)
            opcode, payload = ws.recv()
            check("binary frames echo through the tunnel",
                  opcode == 0x2 and payload == blob, "opcode=%s len=%d" % (opcode, len(payload)))

            big = os.urandom(1024 * 1024)                        # 64-bit length frame
            ws.send(big, opcode=0x2)
            opcode, payload = ws.recv()
            check("a 1 MiB frame survives (64-bit length)",
                  opcode == 0x2 and payload == big, "opcode=%s len=%d" % (opcode, len(payload)))

            ws.send(b"second")
            first = ws.recv()
            ws.send(b"third")
            second = ws.recv()
            check("consecutive frames stay framed",
                  first == (0x1, b"second") and second == (0x1, b"third"), "%r %r" % (first, second))

            ws.send(b"ping-me", opcode=0x9)
            opcode, payload = ws.recv()
            check("control frames pass through opaquely", (opcode, payload) == (0xA, b"ping-me"),
                  "opcode=%s %r" % (opcode, payload))

            second = fixture.Ws.connect(gw.port, "/up/ws")
            second.send(b"tunnel-two")
            check("a second concurrent tunnel works too", second.recv() == (0x1, b"tunnel-two"))
            time.sleep(0.3)
            tunnels = (gw.health().get("websockets") or {})
            check("healthz counts the tunnels that are open",
                  tunnels.get("open") == 2 and tunnels.get("total") >= 2, json.dumps(tunnels))
            second.close()
            time.sleep(0.5)
            check("a closed tunnel stops being counted open",
                  (gw.health().get("websockets") or {}).get("open") == 1,
                  json.dumps(gw.health().get("websockets")))

            ws.send(b"last words")
            ws.sock.shutdown(socket.SHUT_WR)                     # half close: reads stay open
            opcode, payload = ws.recv()
            tail = b""
            try:
                ws.sock.settimeout(5)
                tail = ws.sock.recv(16)
            except OSError as exc:
                tail = b"<timeout %s>" % str(exc).encode()
            check("a half close still delivers the tail and then EOF",
                  (opcode, payload) == (0x1, b"last words") and tail == b"",
                  "opcode=%s %r tail=%r" % (opcode, payload, tail))
            ws.close()

            refused = fixture.Ws.connect(gw.port, "/plain/ws")
            check("an upgrade on a route that did not opt in is refused with 400",
                  refused.status == 400, "status=%s" % refused.status)
            refused.close()

            for label, key, version in (("a bad key", "not-a-key", "13"),
                                        ("a missing key", "", "13"),
                                        ("the wrong version", None, "8")):
                bad = fixture.Ws.connect(gw.port, "/up/ws", key=key or (base64.b64encode(b"short").decode()),
                                         version=version)
                check("%s is refused with 400" % label, bad.status == 400, "status=%s" % bad.status)
                bad.close()

            no = fixture.Ws.connect(gw.port, "/up/refuse-upgrade")
            check("an upstream that refuses the upgrade is relayed, not faked",
                  no.status == 403, "status=%s" % no.status)
            no.close()

            with urllib.request.urlopen("http://127.0.0.1:%d/up/text" % gw.port, timeout=10) as resp:
                check("plain HTTP still works on a websocket-enabled route",
                      resp.status == 200 and resp.read() == b"hello from upstream\n")

            time.sleep(0.5)
            health = gw.health()
            tunnels = health.get("websockets") or {}
            check("healthz shows every tunnel closed again",
                  tunnels.get("total") == 2 and tunnels.get("open") == 0, json.dumps(tunnels))

            log = gw.tail(400)
            check("every tunnel is audited (open + close with byte counts)",
                  log.count("ws-open") == 2 and log.count("ws-close") == 2
                  and "to_upstream=" in log and "to_client=" in log,
                  "open=%d close=%d" % (log.count("ws-open"), log.count("ws-close")))
            check("refusals are logged too",
                  log.count("ws-refused") >= 2, "refusals=%d" % log.count("ws-refused"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit-only", action="store_true")
    args = parser.parse_args()
    tmp = fixture.WS_ROOT / "tmp" / "gateway-ws-test"
    gateway = load_gateway()
    unit_tests(gateway)
    manifest_tests()
    if not args.unit_only:
        live_tests(tmp)
    print("\nWEBSOCKET: %d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

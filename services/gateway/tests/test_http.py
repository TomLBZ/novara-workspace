#!/usr/bin/env python3
"""Proxying checks for ws-gateway: framing, streaming, request bodies (stdlib only).

  python3 services/gateway/tests/test_http.py [--keep-tmp]

Everything runs against `fixture.Upstream` and a scratch manifest, so the live router and
the live `services/services.json` are untouched.  Framing is asserted on raw sockets, not
just on the decoded body: "the body arrived" is not the same as "a streaming client could
have used it".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import socket
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fixture  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("PASS %s%s" % (name, (" (%s)" % detail) if detail else ""))
    else:
        FAIL += 1
        print("FAIL %s%s" % (name, (" :: %s" % detail) if detail else ""))


def get(port: int, path: str, timeout: float = 30.0):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def raw(port: int, request: bytes) -> fixture.Reader:
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    sock.sendall(request)
    return fixture.Reader(sock)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-tmp", action="store_true")
    args = parser.parse_args()
    tmp = fixture.WS_ROOT / "tmp" / "gateway-http-test"

    dead_port = fixture.free_port()
    with fixture.Upstream() as up:
        routes = [
            {"prefix": "/up", "type": "proxy", "upstream": "http://127.0.0.1:%d" % up.port,
             "strip_prefix": True, "websocket": True},
            {"prefix": "/plain", "type": "proxy", "upstream": "http://127.0.0.1:%d" % up.port},
            {"prefix": "/dead", "type": "proxy", "upstream": "http://127.0.0.1:%d" % dead_port},
            # A route whose responses an intermediate CDN must pass through untouched.
            {"prefix": "/nt", "type": "proxy", "upstream": "http://127.0.0.1:%d" % up.port,
             "strip_prefix": True, "no_transform": True},
        ]
        with fixture.Gateway(tmp, routes) as gw:
            # --- routing + headers -------------------------------------------
            status, headers, body = get(gw.port, "/up/text")
            check("fixed-length response relays", status == 200 and body == b"hello from upstream\n",
                  "status=%s body=%r" % (status, body[:40]))
            check("content-length is passed through", headers.get("Content-Length") == "20",
                  repr(headers.get("Content-Length")))
            check("end-to-end headers survive", headers.get("X-Upstream-Test") == "yes")
            check("hop-by-hop headers are dropped",
                  "Keep-Alive" not in headers and "keep-alive" not in {k.lower() for k in headers})
            check("strip_prefix reaches the upstream as the bare path",
                  up.paths[-1] == "/text", repr(up.paths[-1]))
            check("a route without strip_prefix keeps the prefix",
                  get(gw.port, "/plain/text")[0] == 404 and up.paths[-1] == "/plain/text",
                  repr(up.paths[-1]))

            # --- no_transform (an intermediate CDN must not rewrite these responses) ---
            status, headers, _ = get(gw.port, "/nt/text")
            check("a no_transform route sends Cache-Control: no-transform even when the upstream sent none",
                  status == 200 and headers.get("Cache-Control") == "no-transform",
                  repr(headers.get("Cache-Control")))
            status, headers, _ = get(gw.port, "/nt/cached")
            check("an upstream Cache-Control is merged, not replaced",
                  status == 200 and headers.get("Cache-Control") == "no-store, no-transform",
                  repr(headers.get("Cache-Control")))
            status, headers, _ = get(gw.port, "/up/text")
            check("a route without the flag is left alone",
                  status == 200 and headers.get("Cache-Control") is None,
                  repr(headers.get("Cache-Control")))

            status, _, body = get(gw.port, "/nope/at/all")
            check("unknown prefix is a router 404", status == 404 and b"no route" in body,
                  "status=%s" % status)

            status, _, body = get(gw.port, "/dead/text", timeout=20)
            check("a dead upstream is a 502", status == 502 and b"unreachable" in body,
                  "status=%s body=%r" % (status, body[:60]))

            # --- streaming ----------------------------------------------------
            reader = raw(gw.port, b"GET /up/stream HTTP/1.1\r\nHost: gw\r\n\r\n")
            began = time.time()
            head = reader.line().decode("latin-1")
            while not head.endswith("\r\n\r\n"):
                head += reader.line().decode("latin-1")
            head_time = time.time() - began
            check("the response head is sent before the body ends (no buffering)",
                  head_time < 1.0, "head after %.2fs" % head_time)
            check("a chunked upstream stays chunked",
                  "transfer-encoding: chunked" in head.lower(), head.splitlines()[0] if head else "")
            first = time.time()
            size = int(reader.line().split(b";")[0].strip(), 16)
            piece = reader.take(size)
            reader.take(2)                                     # CRLF that closes the chunk
            check("the first chunk reaches the client on its own",
                  time.time() - first < 0.6 and piece == b"piece-0\n",
                  "%.2fs %r" % (time.time() - first, piece))
            rest = piece
            while True:
                size = int(reader.line().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    reader.line()
                    break
                rest += reader.take(size)
                reader.take(2)
            check("the whole chunked body arrives", rest == b"piece-0\npiece-1\npiece-2\n", repr(rest))

            # --- big bodies ---------------------------------------------------
            size = 4 * 1024 * 1024
            status, _, body = get(gw.port, "/up/big?n=%d" % size, timeout=60)
            check("a 4 MiB response arrives intact",
                  status == 200 and len(body) == size and hashlib.sha256(body).hexdigest()
                  == hashlib.sha256((b"0123456789abcdef" * ((size // 16) + 1))[:size]).hexdigest(),
                  "status=%s len=%d" % (status, len(body)))

            status, headers, body = get(gw.port, "/up/no-length", timeout=20)
            check("an EOF-delimited upstream becomes Connection: close + full body",
                  status == 200 and body == b"until-eof\n", "status=%s body=%r" % (status, body))

            # --- HEAD ---------------------------------------------------------
            sock = socket.create_connection(("127.0.0.1", gw.port), timeout=10)
            sock.sendall(b"HEAD /up/text HTTP/1.1\r\nHost: gw\r\nConnection: close\r\n\r\n")
            head = b""
            while b"\r\n\r\n" not in head:
                piece = sock.recv(4096)
                if not piece:
                    break
                head += piece
            sock.close()
            check("HEAD relays the status and content-length, with no body",
                  b"200 OK" in head and b"content-length: 20" in head.lower()
                  and head.split(b"\r\n\r\n", 1)[1] == b"", repr(head[-60:]))

            # --- request bodies ------------------------------------------------
            payload = bytes(range(256)) * 4096                      # 1 MiB, content-length
            sock = socket.create_connection(("127.0.0.1", gw.port), timeout=30)
            sock.sendall(b"POST /up/echo HTTP/1.1\r\nHost: gw\r\nContent-Type: application/octet-stream\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(payload))
            sock.sendall(payload)
            status, _, body = fixture.read_response(fixture.Reader(sock), until_close=True)
            sock.close()
            got = json.loads(body.decode() or "{}")
            check("a content-length upload reaches the upstream byte-for-byte",
                  status == 200 and got.get("bytes") == len(payload)
                  and got.get("sha256") == hashlib.sha256(payload).hexdigest(), repr(got))

            pieces = [b"A" * 100000, b"BB", b"C" * 3]
            framed = b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in pieces) + b"0\r\n\r\n"
            sock = socket.create_connection(("127.0.0.1", gw.port), timeout=30)
            sock.sendall(b"POST /up/echo HTTP/1.1\r\nHost: gw\r\nTransfer-Encoding: chunked\r\n"
                         b"Connection: close\r\n\r\n" + framed)
            status, _, body = fixture.read_response(fixture.Reader(sock), until_close=True)
            sock.close()
            got = json.loads(body.decode() or "{}")
            whole = b"".join(pieces)
            check("a chunked upload reaches the upstream byte-for-byte",
                  status == 200 and got.get("bytes") == len(whole)
                  and got.get("sha256") == hashlib.sha256(whole).hexdigest(), repr(got))

            # --- healthz -------------------------------------------------------
            health = gw.health()
            check("healthz still reports status ok + routes",
                  health.get("status") == "ok" and "/up" in health.get("routes", []),
                  json.dumps({k: health.get(k) for k in ("status", "listen", "routes", "websockets")}))

            log = gw.tail(200)
            errors = [line for line in log.splitlines() if "proxy-error" in line]
            check("the only logged error is the dead upstream we asked for",
                  len(errors) == 1 and "/dead/text" in errors[0], "; ".join(errors)[:200])

    if not args.keep_tmp:
        pass
    print("\nHTTP: %d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

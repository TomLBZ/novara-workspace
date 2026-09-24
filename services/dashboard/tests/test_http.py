#!/usr/bin/env python3
"""End-to-end HTTP checks for the dashboard API (stdlib only).

Starts `dashboard.py` on a free loopback port with a scratch config (its own token
file under `tmp/`, so the live one is untouched) and a scratch manifest, exercises
the file-browser API against the real workspace tree read-only, the route-entry
declaration (static / declared-entry / undeclared) and the watcher schedule
normalisation, then stops the server.

  python3 services/dashboard/tests/test_http.py [--ws-root PATH]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.token: str | None = None

    def request(self, path: str, data: bytes | None = None, token: str | None = None):
        url = self.base + path
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        token = token if token is not None else self.token
        if token:
            req.add_header("X-WS-Files-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            try:
                return exc.code, json.loads(body or "{}")
            except ValueError:
                return exc.code, {"error": body[:200]}
        except Exception as exc:  # connection dropped / refused / timeout
            return "conn-error", {"error": "%s: %s" % (type(exc).__name__, exc)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ws-root", default=str(HERE.parents[1]))
    args = parser.parse_args()
    ws_root = pathlib.Path(args.ws_root).resolve()

    work = ws_root / "tmp" / "dashboard-file-browser-test"
    work.mkdir(parents=True, exist_ok=True)
    token_file = work / "token"
    if token_file.exists():
        token_file.unlink()
    port = free_port()
    # Scratch manifest: the tracked example's dashboard entry, with a free port and a
    # token file under tmp/ so the live manifest and token stay untouched.
    base = json.loads((ws_root / "services" / "services.example.json").read_text())["dashboard"]
    settings = dict(base.get("settings") or {})
    settings["files"] = {**(settings.get("files") or {}),
                         "token_file": str(token_file.relative_to(ws_root))}
    manifest_path = work / "services.json"
    dead = free_port()
    manifest_path.write_text(json.dumps({
        "_about": "scratch manifest for tests/test_http.py",
        "dashboard": {**base, "port": port, "settings": settings},
        # A service that nothing is listening for: the route can be asked about nothing at all.
        "ghost": {"script": "services/ghost/ghost.py", "port": dead, "health": "/healthz",
                  "log": "logs/ghost.log"},
        # Route table fixtures: a static route (no service to ask), a proxy route whose app cannot
        # answer a route table but that declares its own `entry`, and one that declares nothing.
        "gateway": {
            "script": "services/gateway/gateway.py", "port": free_port(), "health": "/healthz",
            "log": "logs/gateway.log",
            "routes": [
                {"prefix": "/projects/hello", "type": "static", "root": "services/sites/hello"},
                {"prefix": "/front-door", "type": "proxy", "service": "ghost", "entry": "/front-door/"},
                {"prefix": "/no-entry", "type": "proxy", "service": "ghost"},
            ],
        },
    }))

    # Watcher fixtures: the two schedule shapes the API has to present in one vocabulary.
    home = work / "hermes-home"
    (home / "cron").mkdir(parents=True, exist_ok=True)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
        {"id": "j1", "name": "minutely", "schedule": {"kind": "cron", "expr": "* * * * *",
                                                     "display": "* * * * *"},
         "schedule_display": "* * * * *", "last_run_at": "2026-09-24T10:10:19.473769+00:00",
         "last_status": "ok", "enabled": True},
        {"id": "j2", "name": "five-minutely", "schedule": {"kind": "cron", "expr": "*/5 * * * *",
                                                          "display": "*/5 * * * *"},
         "schedule_display": "*/5 * * * *", "last_run_at": "2026-09-24T10:05:19.852731+00:00",
         "last_status": "ok", "enabled": True},
        {"id": "j3", "name": "quarter-hourly", "schedule": {"kind": "interval", "minutes": 15,
                                                           "display": "every 15m"},
         "schedule_display": "every 15m", "last_run_at": "2026-09-24T09:57:18.100000+00:00",
         "last_status": "ok", "enabled": True},
    ]}))

    env = {"PATH": "/usr/bin:/bin", "WS_MANIFEST": str(manifest_path), "WS_SERVICE": "dashboard",
           "WS_PID_FILE": str(work / "dashboard.pid"), "HERMES_HOME": str(home)}
    server_log = work / "server.log"
    log_handle = open(server_log, "w")
    proc = subprocess.Popen([sys.executable, str(HERE / "dashboard.py")], env=env,
                            stdout=log_handle, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    failures: list[str] = []
    checks = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal checks
        checks += 1
        if condition:
            print(f"PASS  {label}")
        else:
            failures.append(label)
            print(f"FAIL  {label} {detail}")

    try:
        client = Client(base)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                status, _ = client.request("/api/health")
                if status == 200:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            print("FAIL  server did not answer /api/health within 15s")
            print(server_log.read_text()[-2000:] if server_log.exists() else "")
            return 1

        status, body = client.request("/api/files/access")
        check("access (anonymous) reports the projects scope",
              status == 200 and body.get("scope") == "root" and body.get("label") == "projects"
              and body.get("authenticated") is False, str(body))

        status, body = client.request("/api/files/list?path=")
        names = [e["name"] for e in body.get("entries", [])]
        check("listing the default root works", status == 200 and isinstance(names, list), str(body)[:200])
        check("listing hides nothing outside the root",
              all((ws_root / "projects" / n).exists() for n in names), str(names))

        for probe, expected in (("?path=..", 400), ("?path=../config.yaml", 400),
                                ("?path=/etc/passwd", 404), ("?path=../../etc", 400)):
            status, body = client.request("/api/files/list" + probe)
            check(f"traversal refused: {probe}",
                  status == expected and "entries" not in body, f"{status} {body}")

        status, body = client.request("/api/files/read?path=config.yaml")
        check("secrets are unreachable without the token",
              status == 400 and ("browse root" in body.get("error", "") or "deny list" in body.get("error", ""))
              and body.get("content") is None, f"{status} {body}")

        status, body = client.request("/api/files/unlock", data=json.dumps({"token": "wrong"}).encode())
        check("wrong token is rejected", status == 401 and body.get("attempts_left", 0) >= 1, f"{status} {body}")

        token = token_file.read_text().strip()
        check("token file exists with mode 600",
              token_file.exists() and (token_file.stat().st_mode & 0o777) == 0o600)

        status, body = client.request("/api/files/unlock", data=json.dumps({"token": token}).encode())
        check("correct token unlocks the workspace scope",
              status == 200 and body.get("authenticated") is True and body.get("label") == "workspace",
              f"{status} {body}")

        client.token = token
        status, body = client.request("/api/files/list?path=")
        rows = {e["name"]: e for e in body.get("entries", [])}
        check("admin scope lists the workspace root",
              status == 200 and "projects" in rows and body.get("scope") == "admin", str(list(rows))[:200])
        check("sensitive entries are marked denied",
              rows.get("config.yaml", {}).get("denied") is True, str(rows.get("config.yaml")))

        for rel in ("config.yaml", ".env", "config/keys", ".git/config"):
            status, body = client.request("/api/files/read?path=" + rel)
            check(f"deny list holds for {rel}",
                  status == 400 and "deny list" in body.get("error", ""), f"{status} {body}")

        status, body = client.request("/api/files/read?path=README.md")
        check("admin can preview a real text file",
              status == 200 and body.get("kind") == "text" and "workspace" in (body.get("content") or "")[:400],
              f"{status} {body.get('kind')}")

        status, body = client.request("/api/files/raw?path=README.md")
        check("raw preview refuses non-whitelisted types", status == 415, f"{status} {body}")

        status, body = client.request("/api/files/list?path=", token="bogus")
        check("a bogus header token is rejected with 401", status == 401 and "attempts_left" in body,
              f"{status} {body}")

        status, body = client.request("/api/status")
        check("the existing status API still answers", status == 200 and "services" in body,
              f"{status} {str(body)[:200]}")

        routes = {r["prefix"]: r for r in body.get("routes", [])}
        check("a static route links to its own prefix",
              routes.get("/projects/hello", {}).get("link") == "/projects/hello/"
              and routes["/projects/hello"]["entry_source"] == "manifest:gateway.routes.prefix",
              str(routes.get("/projects/hello")))
        check("a route that declares its own entry lights up like the others",
              routes.get("/front-door", {}).get("link") == "/front-door/"
              and routes["/front-door"]["entry_source"] == "manifest:gateway.routes.entry",
              str(routes.get("/front-door")))
        check("a route that declares nothing stays plain text and says why",
              routes.get("/no-entry", {}).get("link") is None
              and routes["/no-entry"]["entry_source"] == "routes-unreachable",
              str(routes.get("/no-entry")))

        watchers = {w["name"]: w for w in body.get("watchers", [])}
        check("every watcher's schedule has the same shape",
              len(watchers) == 3
              and all(set(w["schedule"]) == {"kind", "every_seconds", "raw", "display"}
                      for w in watchers.values()), str(body.get("watchers"))[:200])
        check("a cron job and an interval job are worded the same way",
              [watchers[n]["schedule"]["display"] for n in ("minutely", "five-minutely", "quarter-hourly")]
              == ["every 1 min", "every 5 min", "every 15 min"], str(body.get("watchers"))[:300])
        check("the period and the source expression both survive",
              [watchers[n]["schedule"]["every_seconds"] for n in ("minutely", "five-minutely", "quarter-hourly")]
              == [60, 300, 900]
              and watchers["five-minutely"]["schedule"]["raw"] == "*/5 * * * *"
              and watchers["quarter-hourly"]["schedule"]["kind"] == "interval",
              str(body.get("watchers"))[:300])
        check("last run times are normalised to HH:MM:SSZ",
              watchers["minutely"]["last_run_at"] == "10:10:19Z", str(watchers.get("minutely")))
        status, body = Client(base).request("/api/files/access")   # anonymous client
        check("manifest settings reach the file browser (root=projects)",
              status == 200 and body.get("browse_root") == "projects", str(body))

        # lockout: four more failures trip the five-failure guard, and it must stay tripped.
        for _ in range(5):
            client.request("/api/files/unlock", data=json.dumps({"token": "wrong"}).encode())
        status, body = client.request("/api/files/unlock", data=json.dumps({"token": token}).encode())
        check("repeated failures lock the endpoint", status == 429 and body.get("locked_for", 0) > 0,
              f"{status} {body}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_handle.close()
        if failures:
            print("--- server log tail ---")
            print("".join(server_log.read_text().splitlines(True)[-15:]))
        for leftover in (token_file, manifest_path, work / "dashboard.pid"):
            try:
                leftover.unlink()
            except OSError:
                pass

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("failed: " + "; ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

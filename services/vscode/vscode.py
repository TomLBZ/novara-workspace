#!/usr/bin/env python3
"""ws-vscode - VS Code in the browser (code-server) behind the workspace router.

The service is a supervisor around the vendored `code-server` binary
(`runtime/code-server/current`, installed by `tools/bootstrap.sh`).  It is reachable only
through `ws-gateway` on the `/vscode` prefix (`services.json` -> `gateway.routes`, with
`"websocket": true` because the workbench keeps a websocket open to the extension host):

    http://<public host>/vscode/          login page -> the editor
    http://<public host>/vscode/?folder=/workspace/projects/quotagent

Why a path prefix works without a base-path flag: code-server serves the workbench with
*relative* URLs and its IDE socket under `stable-<hash>`, so keeping the prefix in the
upstream request (route without `strip_prefix`) is enough.  Two things are load-bearing:

* `--trusted-origins <host>` for every public hostname - code-server 403s an upgrade whose
  `Origin` does not match the `Host` it is asked under, and behind a proxy it always sees a
  different host.  Without this the page loads but the workbench never connects.
* `--auth password` with the password from `config.yaml` -> `vscode.password`, else a
  generated 0600 file (`config/vscode-password`).  Never `--auth none`: the prefix is
  publicly reachable through Nginx Proxy Manager.
* the workbench ships a **nonce CSP** (`script-src 'self' 'unsafe-eval' blob: 'nonce-…'`), so the
  `/vscode` route sets `"no_transform": true`: the router then sends `Cache-Control: no-transform`,
  which stops Cloudflare's Rocket Loader from rewriting the page.  Without it Rocket Loader injects
  a loader whose inline activation script has no nonce, the CSP blocks it, no script runs and the
  workbench is a blank page.

Every state file (user data, extensions, HOME) stays inside `runtime/code-server/`, so the
whole service travels with the workspace bind mount.

    services/vscode/vscode.py --check            # resolved manifest entry + settings
    services/vscode/vscode.py --password         # print it (generates on first call)
    services/vscode/vscode.py --password --rotate
    bin/ws-gateway start|stop|restart vscode     # managed (the cron watchdog runs `ensure`)
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
WS_ROOT = HERE.parents[1]
sys.path.insert(0, str(WS_ROOT / "tools"))
import servicemanifest as sm  # noqa: E402  (workspace tool, stdlib only)
import wsconfig  # noqa: E402  (workspace tool: config.yaml reader)

SERVICE = os.environ.get("WS_SERVICE", "vscode")
MANIFEST = sm.load(WS_ROOT)
ENTRY = sm.entry(MANIFEST, SERVICE)
CFG = sm.settings(MANIFEST, SERVICE)
PID_FILE = pathlib.Path(os.environ.get("WS_PID_FILE", str(WS_ROOT / "runtime" / "run" / "vscode.pid")))

CODE_SERVER = WS_ROOT / str(CFG.get("code_server", "runtime/code-server/current/bin/code-server"))
RUNTIME = WS_ROOT / str(CFG.get("runtime_dir", "runtime/code-server"))
PASSWORD_FILE = WS_ROOT / str(CFG.get("password_file", "config/vscode-password"))
FOLDER = str(CFG.get("folder", "."))
TRUSTED_ORIGINS = [str(o) for o in (CFG.get("trusted_origins") or [])]


# --- the password -----------------------------------------------------------
def config_password() -> str:
    try:
        return str(wsconfig.get(wsconfig.load(), "vscode.password") or "").strip()
    except Exception:                                   # unreadable/absent config.yaml
        return ""


def ensure_password() -> tuple[str, str]:
    """Return `(password, source)`; generate + persist a 0600 file when none is configured."""
    configured = config_password()
    if configured:
        return configured, "config.yaml"
    try:
        existing = PASSWORD_FILE.read_text().strip()
    except OSError:
        existing = ""
    if existing:
        return existing, str(PASSWORD_FILE.relative_to(WS_ROOT))
    password = secrets.token_urlsafe(18)
    PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(password + "\n")
    os.chmod(PASSWORD_FILE, 0o600)
    return password, str(PASSWORD_FILE.relative_to(WS_ROOT))


def rotate_password() -> tuple[str, str]:
    try:
        PASSWORD_FILE.unlink()
    except FileNotFoundError:
        pass
    return ensure_password()


# --- process plumbing -------------------------------------------------------
def workspace_path() -> str:
    """PATH of a `bin/activate.sh` shell: the integrated terminal gets the toolchain."""
    try:
        out = subprocess.run(["bash", "-c", '. "%s/bin/activate.sh" >/dev/null 2>&1; printf %%s "$PATH"'
                              % WS_ROOT], capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def child_env(password: str) -> dict:
    home = RUNTIME / "home"
    env = dict(os.environ, PASSWORD=password, HOME=str(home),
               XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
               XDG_CACHE_HOME=str(home / "cache"), XDG_STATE_HOME=str(home / "state"))
    env.pop("VIRTUAL_ENV", None)
    path = workspace_path()
    if path:
        env["PATH"] = path
    return env


def command(port: int) -> list[str]:
    args = [str(CODE_SERVER), "--auth", "password", "--bind-addr", "127.0.0.1:%d" % port,
            "--user-data-dir", str(RUNTIME / "data"), "--extensions-dir", str(RUNTIME / "extensions"),
            "--disable-telemetry", "--disable-update-check"]
    if CFG.get("disable_workspace_trust", True):
        args.append("--disable-workspace-trust")     # the folder is this workspace, not a stranger's
    for origin in TRUSTED_ORIGINS:
        args += ["--trusted-origins", origin]
    if FOLDER:
        args.append(str(WS_ROOT / FOLDER) if FOLDER != "." else str(WS_ROOT))
    return args


def probe_health(port: int) -> int:
    """Healthy = code-server's HTTP server answers `/healthz`.

    The body's `status` describes *IDE activity*, not the server: code-server skips its own
    heartbeat for `/healthz` requests ("otherwise health checks will make it look like
    code-server is always in use"), so an idle editor legitimately answers
    `{"status":"expired","lastHeartbeat":0}`.  Reading that as DOWN would make the watchdog
    restart the service - and the user's session - every minute, so only the answer itself
    counts here.
    """
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=3) as resp:
            status = resp.status
            body = json.loads(resp.read().decode() or "{}")
    except Exception:
        return 1
    return 0 if status == 200 and "status" in body else 1


def main() -> int:
    ports = sm.ports(ENTRY)
    if not ports:
        print("ws-vscode: the manifest entry needs 'port'", flush=True)
        return 2
    port = ports[0]
    if not CODE_SERVER.is_file():
        print("ws-vscode: code-server is not installed at %s - run tools/bootstrap.sh" % CODE_SERVER,
              flush=True)
        return 2
    password, source = ensure_password()
    for directory in (RUNTIME / "data", RUNTIME / "extensions", RUNTIME / "home"):
        directory.mkdir(parents=True, exist_ok=True)

    print("%s ws-vscode up: pid=%d port=%d folder=%s trusted_origins=%s password_source=%s "
          "(print the password with: %s --password)" % (
              time.strftime("%Y-%m-%dT%H:%M:%S%z"), os.getpid(), port, FOLDER,
              ",".join(TRUSTED_ORIGINS) or "none", source, pathlib.Path(__file__).name), flush=True)
    child = subprocess.Popen(command(port), env=child_env(password), cwd=str(WS_ROOT))
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.is_set():
            if child.poll() is not None:
                print("ws-vscode: code-server exited (rc=%s) - the watchdog restarts the service"
                      % child.returncode, flush=True)
                return child.returncode or 1
            stop.wait(1.0)
        print("ws-vscode: stopping (code-server pid %s)" % child.pid, flush=True)
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
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
        print("binary:   %s (%s)" % (CODE_SERVER, "present" if CODE_SERVER.is_file() else "MISSING"))
        print("settings: %s" % json.dumps(CFG, sort_keys=True))
        print("password: source=%s" % ensure_password()[1])
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "--password":
        password, source = rotate_password() if "--rotate" in sys.argv else ensure_password()
        print(password)
        print("# source: %s" % source, file=sys.stderr)
        sys.exit(0)
    sys.exit(main())

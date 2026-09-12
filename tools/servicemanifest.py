#!/usr/bin/env python3
"""The service manifest reader/validator - the single source of service truth.

`services/services.json` (machine-local; tracked baseline `services.example.json`) owns
everything about the workspace's long-running services: script, ports, health path, log
file, the router's route table, and each service's own settings. Credentials, identity and
LLM preferences live in `config.yaml` - nothing is configured in two files.

Consumers: `bin/ws-gateway` (start/stop/ensure/status), `services/gateway/gateway.py`
(routes + listen ports) and `services/dashboard/dashboard.py` (port + settings). All three
go through this module so the schema is parsed and validated in exactly one place.

CLI (used by the shell scripts, and handy by hand):

    tools/servicemanifest.py validate [--json]     # exit 3 when the manifest has problems
    tools/servicemanifest.py services              # one service name per line
    tools/servicemanifest.py entry <name>          # that entry as JSON, ports resolved
    tools/servicemanifest.py ports|script|log|health|settings <name>
    tools/servicemanifest.py routes                # resolved route table (JSON)
    tools/servicemanifest.py listen                # the router's listen ports

Env: `WS_ROOT` (defaults to the workspace above this file), `WS_MANIFEST` (alternate file,
which is how the tests stay away from the live manifest).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

SCHEMA_NOTE = (
    "services.json owns: services (script/port|listen/health/log), the gateway's routes, "
    "and per-service settings. config.yaml owns credentials/identity/LLM. "
    "Top-level keys starting with '_' are comments and are ignored."
)
ROUTE_TYPES = ("static", "proxy")


class ManifestError(Exception):
    """Unreadable or structurally invalid manifest - message is meant for a human."""


def root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("WS_ROOT") or pathlib.Path(__file__).resolve().parents[1]).resolve()


def path(ws_root: pathlib.Path | None = None) -> pathlib.Path:
    override = os.environ.get("WS_MANIFEST")
    if override:
        return pathlib.Path(override).expanduser().resolve()
    return (ws_root or root()) / "services" / "services.json"


def load(ws_root: pathlib.Path | None = None) -> dict:
    """Manifest with `_`-prefixed comment keys stripped."""
    manifest_path = path(ws_root)
    if not manifest_path.exists():
        raise ManifestError("missing %s (a fresh clone copies services.example.json over it)" % manifest_path)
    try:
        raw = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise ManifestError("%s is not readable JSON: %s" % (manifest_path, exc))
    if not isinstance(raw, dict):
        raise ManifestError("%s must contain a JSON object of services" % manifest_path)
    return {k: v for k, v in raw.items() if not str(k).startswith("_")}


def comments(raw_path: pathlib.Path | None = None) -> dict:
    """`_`-prefixed top-level keys of the on-disk file (documentation, not configuration)."""
    try:
        raw = json.loads((raw_path or path()).read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if str(k).startswith("_")} if isinstance(raw, dict) else {}


def services(manifest: dict) -> list[str]:
    return [name for name, entry in manifest.items() if isinstance(entry, dict)]


def ports(entry: dict) -> list[int]:
    """Listen/probe ports: `listen: [..]` wins, else a single `port`."""
    raw = entry.get("listen") if entry.get("listen") else entry.get("port")
    if isinstance(raw, int):
        raw = [raw]
    out = []
    for value in raw or []:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def entry(manifest: dict, name: str) -> dict:
    found = manifest.get(name)
    if not isinstance(found, dict):
        raise ManifestError("no service %r in the manifest (have: %s)" % (name, ", ".join(services(manifest))))
    return found


def settings(manifest: dict, name: str) -> dict:
    value = entry(manifest, name).get("settings")
    return value if isinstance(value, dict) else {}


def routes(manifest: dict, name: str = "gateway") -> list[dict]:
    """Resolved route table: a proxy route's `service` becomes `upstream`."""
    resolved: list[dict] = []
    for route in entry(manifest, name).get("routes", []) or []:
        row = dict(route)
        target = row.pop("service", None)
        if target and not row.get("upstream"):
            port = ports(entry(manifest, str(target)))
            if not port:
                raise ManifestError("route %r points at service %r, which has no port"
                                    % (row.get("prefix"), target))
            row["upstream"] = "http://127.0.0.1:%d" % port[0]
            row["service"] = target
        resolved.append(row)
    return resolved


def problems(manifest: dict) -> list[str]:
    """Structural problems, in human language. Empty list = valid."""
    found: list[str] = []
    if not manifest:
        return ["no services defined (top-level object is empty)"]
    for name, spec in manifest.items():
        if not isinstance(spec, dict):
            found.append("%s: expected an object" % name)
            continue
        if not isinstance(spec.get("script"), str) or not spec["script"]:
            found.append("%s: needs a 'script' path relative to the workspace" % name)
        health = spec.get("health")
        if not isinstance(health, str) or not health.startswith("/"):
            found.append("%s: needs a 'health' path starting with '/'" % name)
        if not isinstance(spec.get("log"), str) or not spec["log"]:
            found.append("%s: needs a 'log' path relative to the workspace" % name)
        if not ports(spec):
            found.append("%s: needs 'port' (int) or 'listen' (list of ints)" % name)
        if spec.get("settings") is not None and not isinstance(spec["settings"], dict):
            found.append("%s: 'settings' must be an object" % name)
        for i, route in enumerate(spec.get("routes", []) or []):
            where = "%s.routes[%d]" % (name, i)
            if not isinstance(route, dict):
                found.append("%s: expected an object" % where)
                continue
            prefix = route.get("prefix")
            if not isinstance(prefix, str) or not prefix.startswith("/"):
                found.append("%s: needs a 'prefix' starting with '/'" % where)
            kind = route.get("type")
            if kind not in ROUTE_TYPES:
                found.append("%s: 'type' must be one of %s" % (where, "|".join(ROUTE_TYPES)))
            elif kind == "static":
                if not isinstance(route.get("root"), str) or not route["root"]:
                    found.append("%s: a static route needs 'root'" % where)
            else:
                target, upstream = route.get("service"), route.get("upstream")
                if target and target not in manifest:
                    found.append("%s: 'service' %r is not a service in this file" % (where, target))
                elif target and not ports(manifest.get(target) or {}):
                    found.append("%s: service %r has no port to proxy to" % (where, target))
                elif not target and not (isinstance(upstream, str) and upstream.startswith("http")):
                    found.append("%s: a proxy route needs 'service' (a service name) "
                                 "or 'upstream' (http://host:port)" % where)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="read/validate the service manifest in services/services.json")
    parser.add_argument("--root", help="workspace root (default: $WS_ROOT or the tree above this file)")
    parser.add_argument("--manifest", help="alternate manifest file (same as $WS_MANIFEST)")
    parser.add_argument("command", choices=("validate", "services", "entry", "ports", "script", "log",
                                            "health", "settings", "routes", "listen", "path", "notes"))
    parser.add_argument("name", nargs="?", help="service name (for entry/ports/script/log/health/settings)")
    parser.add_argument("--json", action="store_true", help="machine-readable output for validate")
    args = parser.parse_args(argv)

    ws_root = pathlib.Path(args.root).resolve() if args.root else root()
    if args.manifest:
        os.environ["WS_MANIFEST"] = args.manifest
    if args.command == "path":
        print(path(ws_root))
        return 0
    try:
        manifest = load(ws_root)
    except ManifestError as exc:
        if args.json:
            print(json.dumps({"ok": False, "problems": [str(exc)], "manifest": str(path(ws_root))}))
        else:
            print("ws-manifest: %s" % exc, file=sys.stderr)
        return 3
    if args.command == "notes":
        print(json.dumps(comments(path(ws_root)), indent=2))
        return 0
    if args.command == "validate":
        found = problems(manifest)
        if args.json:
            print(json.dumps({"ok": not found, "manifest": str(path(ws_root)),
                              "services": services(manifest), "problems": found}, indent=2))
        elif found:
            print("services.json has %d problem(s):" % len(found), file=sys.stderr)
            for line in found:
                print("  - %s" % line, file=sys.stderr)
            print("baseline to restore: cp services/services.example.json services/services.json", file=sys.stderr)
        else:
            print("services.json OK: %s" % ", ".join(services(manifest)))
        return 0 if not found else 3

    try:
        if args.command == "services":
            print("\n".join(services(manifest)))
        elif args.command == "routes":
            print(json.dumps(routes(manifest), indent=2))
        elif args.command == "listen":
            print(" ".join(str(p) for p in ports(entry(manifest, "gateway"))))
        elif args.command == "entry":
            spec = dict(entry(manifest, args.name or ""))
            spec["ports"] = ports(spec)
            print(json.dumps(spec, indent=2))
        elif args.command == "settings":
            print(json.dumps(settings(manifest, args.name or ""), indent=2))
        elif args.command in ("ports", "script", "log", "health"):
            spec = entry(manifest, args.name or "")
            if args.command == "ports":
                print(" ".join(str(p) for p in ports(spec)))
            else:
                print(spec.get(args.command) or "")
    except ManifestError as exc:
        print("ws-manifest: %s" % exc, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

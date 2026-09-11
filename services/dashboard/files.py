#!/usr/bin/env python3
"""Read-only file browser backend for ws-dashboard (stdlib only).

Two scopes:

* **anonymous** - a single configured directory, `files.root` (default `projects`).
* **admin**     - the whole workspace, unlocked by sending the admin token.

The admin token is read, in order, from `config.yaml` -> `dashboard.admin_token`
(workspace config, mode 0600) and otherwise from a generated 0600 file
(`files.token_file`, default `config/dashboard-admin-token`). Both are
gitignored; `dashboard.py --files-token [--rotate]` prints or rotates the
generated one.

Hard rules, applied in BOTH scopes:

* every request resolves to a real path under its browse root (`resolve()` +
  containment), so `..`, absolute paths and symlinks cannot escape;
* a deny list keeps secrets unreadable even for an unlocked caller
  (`config.yaml`, `config/keys/**`, `.git/**`, `.env*`, `*.pem`, `*.key`, ...);
* nothing is ever written - the API is read-only;
* listings and previews are size-capped (`max_entries`, `max_preview_bytes`).
"""
from __future__ import annotations

import fnmatch
import hmac
import os
import pathlib
import secrets
import time

# Deny patterns are workspace-relative POSIX strings. Each pattern is tested three
# ways: exact path, directory prefix, and basename glob (so `*.key` also catches a
# key buried in a project directory).
DENY_PATTERNS = (
    "config.yaml",
    "config/keys",
    "config/git-credentials",
    ".git",
    ".ssh",
    ".env",
    ".env.local",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_rsa.pub",
    "id_ed25519",
    "id_ed25519.pub",
)

# Inline raw preview is limited to a few small, safe-to-render types.
RAW_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml", ".pdf": "application/pdf",
    ".ico": "image/x-icon",
}
TEXT_SNIFF = 4096
LOCKOUT_FAILURES = 5
LOCKOUT_SECONDS = 300.0


def _within(path: pathlib.Path, root: pathlib.Path) -> bool:
    """True when *path* is *root* or lies below it (both already resolved)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False



def _nested_scalar(path: pathlib.Path, dotted: str) -> str:
    """Value of `a.b.c: v` in a simple YAML file, "" when absent/unreadable."""
    parts = dotted.split(".")
    stack: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        if not key or key.startswith("-") or " " in key:
            continue
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if [k for _, k in stack] + [key] == parts:
            return value.strip().strip("'\"")
        if not value.strip():
            stack.append((indent, key))
    return ""


class FileBrowser:
    """Scoped, deny-listed, read-only access to a directory tree."""

    def __init__(self, ws_root: pathlib.Path, cfg: dict) -> None:
        self.ws_root = pathlib.Path(ws_root).resolve()
        cfg = cfg or {}
        self.rel_root = str(cfg.get("root", "projects")).strip() or "projects"
        self.rel_admin_root = str(cfg.get("admin_root", ".")).strip() or "."
        self.max_entries = int(cfg.get("max_entries", 2000) or 2000)
        self.max_preview = int(cfg.get("max_preview_bytes", 262144) or 0)
        self.max_raw = int(cfg.get("max_raw_bytes", 1048576) or 0)
        self.token_rel = str(cfg.get("token_file", "config/dashboard-admin-token")).strip()
        self.deny = tuple(cfg.get("deny_extra") or ()) + DENY_PATTERNS
        self._failures: dict[str, list] = {}

    # ---- roots -------------------------------------------------------------
    def root_for(self, scope: str) -> pathlib.Path:
        rel = self.rel_admin_root if scope == "admin" else self.rel_root
        return (self.ws_root / rel).resolve()

    def scope_for(self, token: str | None) -> str:
        return "admin" if self.check_token(token) else "root"

    def describe(self, scope: str) -> dict:
        root = self.root_for(scope)
        try:
            label = root.relative_to(self.ws_root).as_posix() or "."
        except ValueError:
            label = str(root)
        return {
            "scope": scope,
            "authenticated": scope == "admin",
            "label": "workspace" if scope == "admin" else label,
            "browse_root": label,
            "max_preview_bytes": self.max_preview,
            "max_raw_bytes": self.max_raw,
        }

    # ---- token -------------------------------------------------------------
    def token_file(self) -> pathlib.Path:
        return (self.ws_root / self.token_rel).resolve()

    def config_token(self) -> str:
        """`config.yaml` -> `dashboard.admin_token`, or "" when unset.

        Read with a targeted reader instead of a YAML library: the service stays
        stdlib-only, and a missing PyYAML can never take the dashboard down. Only
        simple `key: value` nesting is understood, which is all this key needs -
        anything fancier falls back to the generated token file.
        """
        return _nested_scalar(self.ws_root / "config.yaml", "dashboard.admin_token")

    def ensure_token(self) -> tuple[str, str]:
        """Return `(token, source)`; generate + persist a file token when none is configured."""
        configured = self.config_token()
        if configured:
            return configured, "config.yaml"
        path = self.token_file()
        try:
            existing = path.read_text().strip()
        except OSError:
            existing = ""
        if existing:
            return existing, str(path.relative_to(self.ws_root))
        token = secrets.token_urlsafe(24)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token + "\n")
        os.chmod(path, 0o600)
        return token, str(path.relative_to(self.ws_root))

    def rotate_token(self) -> tuple[str, str]:
        """Force a new file token (ignored when config.yaml pins one)."""
        path = self.token_file()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return self.ensure_token()

    def check_token(self, supplied: str | None) -> bool:
        if not supplied:
            return False
        token, _ = self.ensure_token()
        return bool(token) and hmac.compare_digest(str(supplied), token)

    # ---- lockout -----------------------------------------------------------
    def locked(self, client: str) -> float:
        """Seconds left in a lockout for *client*, 0 when it may try."""
        fails, until = self._failures.get(client, [0, 0.0])
        return max(0.0, until - time.time())

    def note_failure(self, client: str) -> int:
        entry = self._failures.setdefault(client, [0, 0.0])
        entry[0] += 1
        if entry[0] >= LOCKOUT_FAILURES:
            entry[1] = time.time() + LOCKOUT_SECONDS
            entry[0] = 0
        return max(0, LOCKOUT_FAILURES - entry[0])

    def note_success(self, client: str) -> None:
        self._failures.pop(client, None)

    # ---- paths -------------------------------------------------------------
    def denied(self, rel: str) -> bool:
        """True when a workspace-relative path is on the deny list."""
        rel = rel.strip("/")
        base = rel.rsplit("/", 1)[-1]
        for pattern in self.deny:
            pattern = str(pattern).strip("/")
            if not pattern:
                continue
            if rel == pattern or rel.startswith(pattern + "/"):
                return True
            if fnmatch.fnmatch(base, pattern) or fnmatch.fnmatch(rel, pattern):
                return True
        return False

    def resolve(self, rel: str, scope: str) -> tuple[pathlib.Path | None, str | None]:
        """`(path, None)` or `(None, reason)` for a request path inside *root_for(scope)*."""
        raw = (rel or "").strip()
        if "\x00" in raw:
            return None, "invalid path"
        if len(raw) > 1024:
            return None, "path too long"
        raw = raw.removeprefix("/")
        root = self.root_for(scope)
        try:
            target = (root / raw).resolve() if raw else root
        except (OSError, RuntimeError):
            return None, "path cannot be resolved"
        if not _within(target, root):
            return None, "outside the browse root"
        try:
            rel_ws = target.relative_to(self.ws_root).as_posix()
        except ValueError:
            rel_ws = ""
        if rel_ws and self.denied(rel_ws):
            return None, "path is on the deny list"
        return target, None

    def _entry(self, path: pathlib.Path) -> dict:
        row = {"name": path.name, "type": "file", "size": None, "mtime": None, "denied": False}
        try:
            if path.is_symlink():
                row["symlink"] = True
            st = path.stat()
            row["mtime"] = int(st.st_mtime)
            if path.is_dir():
                row["type"] = "dir"
            else:
                row["size"] = st.st_size
            rel_ws = path.resolve().relative_to(self.ws_root).as_posix()
            row["denied"] = self.denied(rel_ws)
        except (OSError, ValueError):
            row["denied"] = True
        return row

    # ---- API ---------------------------------------------------------------
    def listing(self, rel: str, scope: str) -> tuple[dict | None, str | None, int]:
        path, error = self.resolve(rel, scope)
        if error:
            return None, error, 400 if "browse root" in error or "deny" in error else 404
        if not path.exists():
            return None, "not found", 404
        if not path.is_dir():
            return None, "not a directory", 400
        try:
            children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            return None, f"cannot list: {exc}", 403
        truncated = len(children) > self.max_entries
        entries = [self._entry(child) for child in children[: self.max_entries]]
        rel_posix = "" if path == self.root_for(scope) else path.relative_to(self.root_for(scope)).as_posix()
        payload = {
            "path": rel_posix,
            "parent": ("/" + rel_posix).rsplit("/", 1)[0].lstrip("/") or "",
            "entries": entries,
            "truncated": truncated,
            **self.describe(scope),
        }
        return payload, None, 200

    def preview(self, rel: str, scope: str, max_bytes: int | None = None) -> tuple[dict | None, str | None, int]:
        path, error = self.resolve(rel, scope)
        if error:
            return None, error, 400 if ("browse root" in error or "deny" in error) else 404
        if not path.exists():
            return None, "not found", 404
        if path.is_dir():
            return None, "is a directory", 400
        cap = self.max_preview if not max_bytes else max(1, min(int(max_bytes), self.max_preview))
        try:
            st = path.stat()
            with open(path, "rb") as fh:
                head = fh.read(cap)
        except OSError as exc:
            return None, f"cannot read: {exc}", 403
        payload = {"path": rel.strip("/"), "size": st.st_size, "mtime": int(st.st_mtime),
                   "truncated": st.st_size > len(head), "limit": cap}
        if b"\x00" in head[:TEXT_SNIFF]:
            payload.update({"kind": "binary", "content": None,
                            "note": "binary file - not rendered as text"})
        else:
            payload.update({"kind": "text", "content": head.decode("utf-8", "replace")})
        return payload, None, 200

    def raw(self, rel: str, scope: str) -> tuple[bytes | None, str, str | None, int]:
        """`(body, content_type, error, status)` for small whitelisted asset types."""
        path, error = self.resolve(rel, scope)
        if error:
            return None, "", error, 400 if ("browse root" in error or "deny" in error) else 404
        ctype = RAW_TYPES.get(path.suffix.lower())
        if not path.exists():
            return None, "", "not found", 404
        if ctype is None:
            return None, "", "type not inline-previewable", 415
        try:
            st = path.stat()
            if st.st_size > self.max_raw:
                return None, ctype, f"file larger than {self.max_raw} bytes", 413
            return path.read_bytes(), ctype, None, 200
        except OSError as exc:
            return None, ctype, f"cannot read: {exc}", 403

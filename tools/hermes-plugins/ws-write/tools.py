"""Handlers + schemas for the ws-write plugin.

The built-in ``write_file`` / ``patch`` tools refuse every path outside
``HERMES_WRITE_SAFE_ROOT`` (``/opt/data`` in this container), which blocks the
bind-mounted ``/workspace``. These handlers use plain Python I/O, so an agent
can maintain the workspace without shell heredocs.

Safety is deliberately narrow rather than absent: writes are confined to
``HERMES_WS_WRITE_ROOTS`` (``os.pathsep`` separated, default ``/workspace``) and a
handful of credential-shaped paths stay refused.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
from typing import Any, Dict, Optional

DEFAULT_ROOTS = ("/workspace",)
DENIED_BASENAMES = {"auth.json", ".env", ".env.local", "id_rsa", "id_ed25519"}
DENIED_PARTS = {".ssh"}


def write_roots() -> list[str]:
    raw = os.getenv("HERMES_WS_WRITE_ROOTS", "")
    roots = [os.path.realpath(os.path.expanduser(p)) for p in raw.split(os.pathsep) if p.strip()]
    return roots or [os.path.realpath(p) for p in DEFAULT_ROOTS]


def _fail(error: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error": error, **extra})


def resolve_target(raw: Any) -> tuple[Optional[pathlib.Path], Optional[str]]:
    """Return (path, None) or (None, error). Enforces roots and credential denials."""
    if not isinstance(raw, str) or not raw.strip():
        return None, "path must be a non-empty string"
    path = pathlib.Path(os.path.expanduser(raw.strip()))
    if not path.is_absolute():
        path = pathlib.Path.cwd() / path
    resolved = pathlib.Path(os.path.realpath(path))
    roots = write_roots()
    if not any(resolved == root or str(resolved).startswith(root + os.sep) for root in roots):
        return None, (f"{resolved} is outside the allowed roots "
                      f"({os.pathsep.join(roots)}); set HERMES_WS_WRITE_ROOTS to widen")
    if resolved.name in DENIED_BASENAMES or DENIED_PARTS & set(resolved.parts):
        return None, f"{resolved} looks like a credential store and is refused"
    if resolved.is_dir():
        return None, f"{resolved} is a directory"
    return resolved, None


def write_atomic(path: pathlib.Path, data: str, append: bool = False) -> None:
    """Same-directory temp file + os.replace (atomic); append mode is a plain append."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if append:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(data)
        return
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ws-write-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        stale = pathlib.Path(tmp)
        if stale.exists():
            stale.unlink()
        raise


def _stats(data: str) -> Dict[str, Any]:
    return {"bytes": len(data.encode("utf-8")), "lines": data.count("\n"),
            "sha256": hashlib.sha256(data.encode("utf-8")).hexdigest()[:12]}


def ws_write(params: Dict[str, Any], **kwargs: Any) -> str:
    """Create or replace a file (atomic), or append to it."""
    del kwargs
    path, error = resolve_target(params.get("path"))
    if error:
        return _fail(error)
    content = params.get("content")
    if not isinstance(content, str):
        return _fail("content must be a string (pass \"\" with allow_empty=true to blank a file)")
    append = bool(params.get("append", False))
    if not content and not params.get("allow_empty", False):
        return _fail("empty content refused; pass allow_empty=true if that is intended")
    created = not path.exists()
    try:
        write_atomic(path, content, append=append)
    except OSError as exc:
        return _fail(f"cannot write {path}: {exc}")
    return json.dumps({"success": True, "path": str(path), "created": created,
                       "appended": append, **_stats(content)})


def ws_patch(params: Dict[str, Any], **kwargs: Any) -> str:
    """Replace an exact substring, refusing an ambiguous (multi-hit) match."""
    del kwargs
    path, error = resolve_target(params.get("path"))
    if error:
        return _fail(error)
    if not path.exists():
        return _fail(f"{path} does not exist (ws_write creates files, ws_patch only edits)")
    old = params.get("old_string")
    new = params.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or not old:
        return _fail("old_string and new_string must be strings and old_string must be non-empty")
    if old == new:
        return _fail("old_string and new_string are identical")
    replace_all = bool(params.get("replace_all", False))
    try:
        before = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(f"cannot read {path} as utf-8 text: {exc}")
    hits = before.count(old)
    if hits == 0:
        return _fail("old_string not found in the file")
    if hits > 1 and not replace_all:
        return _fail(f"old_string is not unique ({hits} matches); extend it or pass replace_all=true")
    after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
    try:
        write_atomic(path, after)
    except OSError as exc:
        return _fail(f"cannot write {path}: {exc}")
    return json.dumps({"success": True, "path": str(path), "replacements": hits if replace_all else 1,
                       "bytes_before": len(before.encode("utf-8")), "bytes_after": len(after.encode("utf-8")),
                       "sha256": hashlib.sha256(after.encode("utf-8")).hexdigest()[:12]})


SCHEMAS: Dict[str, Dict[str, Any]] = {
    "ws_write": {
        "name": "ws_write",
        "description": (
            "Write or append to a file outside the Hermes write sandbox (use it for /workspace, "
            "where write_file is denied by HERMES_WRITE_SAFE_ROOT). Replaces the whole file "
            "atomically unless append=true. Refuses empty content unless allow_empty=true, and "
            "refuses credential-shaped paths."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute target path (must sit under the allowed roots, default /workspace)."},
                "content": {"type": "string", "description": "Full file content (or the text to append)."},
                "append": {"type": "boolean", "description": "Append instead of replacing (default false)."},
                "allow_empty": {"type": "boolean", "description": "Permit empty content (default false)."},
            },
            "required": ["path", "content"],
        },
    },
    "ws_patch": {
        "name": "ws_patch",
        "description": (
            "Replace an exact substring in a file outside the Hermes write sandbox (the /workspace "
            "analogue of `patch`). Fails on zero matches and on ambiguous multi-matches unless "
            "replace_all=true; writes atomically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path of an existing text file."},
                "old_string": {"type": "string", "description": "Exact substring to replace; must be unique unless replace_all=true."},
                "new_string": {"type": "string", "description": "Replacement text (may be empty to delete)."},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
}

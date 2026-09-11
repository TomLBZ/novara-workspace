#!/usr/bin/env python3
"""relocate.py - make the workspace's absolute paths match its current location.

Run automatically by bin/activate.sh (self-healing) or manually via `ws-relocate`.
The workspace is designed to be bind-mounted / copied anywhere; this script
repairs the few places where an absolute path is unavoidable:

  * uv's managed-interpreter symlinks   -> rewritten relative
  * venv symlinks / pyvenv.cfg / shebangs of bin/* entry points
  * node `current` + npm global bin symlinks -> rewritten relative
  * the bundled git (conda) env: conda-unpack + binary-safe prefix rewrite

Usage:
    ws-relocate [--dry-run] [--quiet] [--old-root PATH]
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("WS_ROOT") or Path(__file__).resolve().parent.parent).resolve()
LOG: list[str] = []
WARN: list[str] = []

SHEBANG_RE = re.compile(rb"^#!\s*(\S*python[0-9.]*)\s*$")


def log(msg: str, dry: bool = False) -> None:
    LOG.append(("[dry-run] " if dry else "") + msg)


def warn(msg: str) -> None:
    WARN.append(msg)


def fix_symlink(link: Path, dry: bool) -> None:
    """Rewrite absolute symlinks that point inside the workspace -> relative."""
    if not link.is_symlink():
        return
    target = os.readlink(link)
    if not target.startswith("/"):
        return
    if "/workspace/" not in target and "/runtime/" not in target and "/venvs/" not in target:
        return
    new = os.path.relpath(target, link.parent)
    if os.path.normpath(target) == os.path.normpath((link.parent / new).resolve()):
        if new == target:
            return
    log(f"symlink {link.relative_to(ROOT)} -> {new}", dry)
    if not dry:
        link.unlink()
        link.symlink_to(new)


def walk_symlinks(*dirs: Path, recursive: bool = False) -> None:
    for d in dirs:
        if not d.exists():
            continue
        it = d.rglob("*") if recursive else d.glob("*")
        for p in it:
            fix_symlink(p, dry=DRY)


def venv_python_target(python_bin: Path | None, version: str | None) -> Path | None:
    """Pick the *real* interpreter - never a helper script like python3.13-config."""
    if python_bin is None or not python_bin.is_dir():
        return None
    candidates = []
    if version:
        candidates.append(python_bin / f"python{'.'.join(version.split('.')[:2])}")  # 3.13
    candidates.append(python_bin / "python3")
    for cand in candidates:
        if cand.exists():
            return cand
    for cand in sorted(python_bin.glob("python3.*")):
        if re.fullmatch(r"python3(\.[0-9]+)+", cand.name) and cand.exists():
            return cand
    return None


def fix_venv(venv: Path, python_bin: Path | None) -> None:
    """Repair pyvenv.cfg, the python symlinks and bin/* shebangs."""
    cfg = venv / "pyvenv.cfg"
    version = None
    if cfg.exists():
        text = cfg.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^version_info\s*=\s*([0-9.]+)", text, re.M)
        version = m.group(1) if m else None
        if python_bin is None and version:
            cands = sorted((ROOT / "runtime" / "python").glob(f"cpython-{'.'.join(version.split('.')[:2])}*/bin"))
            python_bin = cands[0] if cands else None
        if python_bin:
            new_home = str(python_bin)
            if f"home = {new_home}" not in text:
                text = re.sub(r"^home\s*=.*$", f"home = {new_home}", text, flags=re.M)
                log(f"{cfg.relative_to(ROOT)}: home -> {new_home}", DRY)
                if not DRY:
                    cfg.write_text(text, encoding="utf-8")

    bin_dir = venv / "bin"
    if not bin_dir.is_dir():
        return
    # 1. python / python3 / python3.X -> managed interpreter (relative link)
    py_exe = venv_python_target(python_bin, version)
    if py_exe:
        new = os.path.relpath(py_exe, bin_dir)
        for link in sorted(bin_dir.glob("python*")):
            if link.name not in ("python", "python3", *{f"python{m}" for m in ("3.10", "3.11", "3.12", "3.13", "3.14")}):
                continue
            if link.is_symlink() and os.readlink(link) == new:
                continue
            if not link.is_symlink() and not link.exists():
                continue
            log(f"{link.relative_to(ROOT)} -> {new}", DRY)
            if not DRY:
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(new)
    # 2. entry-point shebangs
    want = f"#!{bin_dir / 'python'}".encode()
    for f in sorted(bin_dir.iterdir()):
        if not f.is_file() or f.is_symlink() or f.name.startswith("python"):
            continue
        if f.stat().st_size > 512 * 1024:
            continue
        try:
            head = f.read_bytes()[:4096]
        except OSError:
            continue
        m = SHEBANG_RE.match(head.split(b"\n", 1)[0])
        if not m:
            continue
        if head.startswith(want):
            continue
        data = f.read_bytes()
        data = data.replace(head.split(b"\n", 1)[0], want, 1)
        log(f"{f.relative_to(ROOT)}: shebang -> {want.decode()}", DRY)
        if not DRY:
            f.write_bytes(data)
            f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def fix_node() -> None:
    node_dir = ROOT / "runtime" / "node"
    if not node_dir.is_dir():
        return
    current = node_dir / "current"
    if current.is_symlink():
        tgt = os.readlink(current)
        base = Path(tgt).name if tgt.startswith("/") else tgt
        if tgt != base:
            log(f"runtime/node/current -> {base}", DRY)
            if not DRY:
                current.unlink()
                current.symlink_to(base)
    walk_symlinks(node_dir / "global" / "bin", node_dir / "global" / "lib", recursive=True)


def fix_git(old_root: Path | None) -> None:
    """Relocate the bundled conda git env (prefix rewrite in text + binaries)."""
    git_dir = ROOT / "runtime" / "git"
    if not git_dir.is_dir():
        return
    unpack = git_dir / "bin" / "conda-unpack"
    if unpack.exists():
        py = which_python()
        if py:
            try:
                res = subprocess.run(
                    [str(py), str(unpack)], capture_output=True, text=True, timeout=300
                )
                if res.returncode == 0:
                    log("runtime/git: conda-unpack OK")
                else:
                    warn(f"conda-unpack exit={res.returncode}: {(res.stderr or res.stdout).strip()[:200]}")
            except Exception as exc:  # pragma: no cover
                warn(f"conda-unpack failed: {exc}")
    if old_root is None or old_root == ROOT:
        return
    old_b, new_b = str(old_root).encode(), str(ROOT).encode()
    longer = len(new_b) > len(old_b)
    if longer:
        warn(
            f"new root is longer than the old one ({old_root} -> {ROOT}); binary-embedded "
            "prefixes are left untouched (they cannot be padded safely). GIT_EXEC_PATH / "
            "GIT_CONFIG_SYSTEM / GIT_TEMPLATE_DIR set by activate.sh keep git fully usable."
        )
    hits = skipped = 0
    for path in git_dir.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if old_b not in data:
            continue
        # binary files can only be rewritten when the new prefix is not longer
        # (NUL padding keeps every fixed-size string intact)
        if longer and b"\0" in data[:8192]:
            skipped += 1
            continue
        hits += 1
        patched = data.replace(old_b, new_b + b"\0" * (len(old_b) - len(new_b)))
        log(f"runtime/git: prefix rewrite in {path.relative_to(git_dir)}", DRY)
        if not DRY:
            path.write_bytes(patched)
            path.chmod(path.stat().st_mode | stat.S_IWUSR)
    if hits:
        log(f"runtime/git: {hits} file(s) carried the old prefix")
    if skipped:
        log(f"runtime/git: {skipped} binary file(s) left with the old prefix (harmless, env vars take precedence)")


def which_python() -> Path | None:
    venv_py = ROOT / "venvs" / "py" / "bin" / "python"
    if venv_py.exists():
        return venv_py
    for cand in sorted((ROOT / "runtime" / "python").glob("cpython-*/bin/python3")):
        if cand.exists():
            return cand
    found = subprocess.run(["bash", "-lc", "command -v python3"], capture_output=True, text=True)
    return Path(found.stdout.strip()) if found.stdout.strip() else None


def managed_python_bin(version_hint: str | None = None) -> Path | None:
    base = ROOT / "runtime" / "python"
    if not base.is_dir():
        return None
    if version_hint:
        for cand in sorted(base.glob(f"cpython-{version_hint}*/bin")):
            return cand
    for cand in sorted(base.glob("cpython-3.13*/bin")):
        return cand
    cands = sorted(base.glob("cpython-*/bin"))
    return cands[0] if cands else None


def state_file() -> Path:
    return ROOT / "runtime" / ".ws-last-root"


def main(argv=None) -> int:
    global DRY
    import argparse

    ap = argparse.ArgumentParser(prog="ws-relocate", description="repair absolute paths after a move")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--old-root", default=None, help="previous workspace root (default: recorded)")
    args = ap.parse_args(argv)
    DRY = args.dry_run

    old_root = Path(args.old_root).resolve() if args.old_root else None
    if old_root is None:
        sf = state_file()
        if sf.exists():
            recorded = sf.read_text(encoding="utf-8").strip()
            if recorded and Path(recorded).resolve() != ROOT:
                old_root = Path(recorded).resolve()

    # 1. uv managed interpreters: absolute key symlinks -> relative
    py_root = ROOT / "runtime" / "python"
    if py_root.is_dir():
        for link in sorted(py_root.iterdir()):
            fix_symlink(link, DRY)

    # 2. virtualenvs
    for venv in sorted((ROOT / "venvs").glob("*")) if (ROOT / "venvs").is_dir() else []:
        fix_venv(venv, managed_python_bin())

    # 3. node + npm global prefix
    fix_node()

    # 4. bundled git
    fix_git(old_root)

    # 5. regenerate the git config (it embeds absolute paths by design)
    if not DRY:
        cfg_yaml = ROOT / "config.yaml"
        ws_config = ROOT / "bin" / "ws-config"
        if cfg_yaml.exists() and ws_config.exists():
            try:
                res = subprocess.run([str(ws_config), "git-setup"], capture_output=True, text=True, timeout=120)
                if res.returncode == 0:
                    log("config/gitconfig regenerated for the current path")
                else:
                    warn(f"ws-config git-setup failed: {(res.stderr or res.stdout).strip()[:200]}")
            except Exception as exc:  # pragma: no cover
                warn(f"ws-config git-setup failed: {exc}")

    # 6. recorded state
    if not DRY:
        state_file().parent.mkdir(parents=True, exist_ok=True)
        state_file().write_text(str(ROOT) + "\n", encoding="utf-8")

    header = f"workspace root: {ROOT}"
    if old_root:
        header += f"   (previous: {old_root})"
    if not args.quiet:
        print(header)
        for line in LOG:
            print(f"  - {line}")
        if not LOG:
            print("  - nothing to repair; paths already consistent")
        for line in WARN:
            print(f"  ! {line}")
    return 0


DRY = False
if __name__ == "__main__":
    raise SystemExit(main())

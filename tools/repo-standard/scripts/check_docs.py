#!/usr/bin/env python3
"""Documentation gate for an agent-handover repository (stdlib only, read-only).

Checks, in one pass:
  1. ID integrity   - every referenced ID is defined where ids_sources says it is
  2. placeholders   - no ID-shaped or literal placeholders left in the docs
  3. budgets        - every file stays inside the limit declared in the standard page
  4. coverage       - every requirement links an acceptance criterion, and no AC is orphaned

Budgets are deliberately NOT configured in this file: they are parsed from the budget table of the
standard page, so the standard stays the single source of truth and cannot drift from the gate.

Configuration: tools/docgate.json, next to this script, is optional; the defaults below match the
layout shipped with the skill. Override the repository root with DOCGATE_ROOT.

Exit codes: 0 pass, 1 failures, 2 configuration/usage error.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("DOCGATE_ROOT") or Path(__file__).resolve().parent.parent).resolve()

DEFAULTS: dict = {
    "id_sources": {
        "FR": "docs/work/functional-requirements.md",
        "V": "docs/work/functional-requirements.md",
        "AC": "docs/work/acceptance-criteria.md",
        "T": "docs/work/progress-checklist.md",
    },
    "adr_dir": "docs/design/adr",
    "evidence_dir": "docs/work/evidence",
    "standard_page": "docs/design/12-documentation-standard.md",
    "scan_suffixes": [".md"],
    "scan_extra_files": ["AGENTS.md", "README.md"],
    "skip_dirs": [".git", "node_modules", "runtime", "venvs", ".venv", "tmp"],
    "coverage": {"requirement": "FR", "acceptance": "AC"},
    "exempt_prefixes": [],
    "placeholder_extra": ["TBD", "TODO-ID"],
}


class Report:
    """Collects `[ok]` / `[FAIL]` lines and the failure count."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.lines: list[str] = []

    def ok(self, text: str) -> None:
        self.lines.append(f"[ok]   {text}")

    def fail(self, text: str, details: list[str] | None = None) -> None:
        self.failures.append(text)
        self.lines.append(f"[FAIL] {text}")
        for detail in details or []:
            self.lines.append(f"       - {detail}")


def load_config() -> dict:
    path = ROOT / "tools" / "docgate.json"
    config = dict(DEFAULTS)
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"docgate: cannot parse {path}: {exc}")
        if not isinstance(user, dict):
            raise SystemExit(f"docgate: {path} must contain a JSON object")
        config.update(user)
    return config


def ref_pattern(prefixes: list[str]) -> re.Pattern[str]:
    """ID reference pattern; longest prefix first so FR- never swallows FRX-."""
    alts = "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True))
    return re.compile(rf"\b(?:{alts})-[A-Z0-9]+(?:-[A-Z0-9]+)*\d\b")


def defined_in(path: Path, prefixes: list[str]) -> set[str]:
    """IDs defined as the first cell of a markdown table row, filtered by prefix."""
    pattern = re.compile(r"^\|\s*(?P<id>[A-Z][A-Z0-9]*-[A-Z0-9-]*\d)\s*\|", re.M)
    found = {m.group("id") for m in pattern.finditer(path.read_text(encoding="utf-8"))}
    return {i for i in found if any(i.startswith(p + "-") for p in prefixes)}


def collect_definitions(rep: Report, config: dict) -> set[str]:
    defined: set[str] = set()
    for prefix, rel in config["id_sources"].items():
        path = ROOT / rel
        if not path.exists():
            rep.fail(f"definition source missing: {rel}")
            continue
        found = defined_in(path, [prefix])
        if not found:
            rep.fail(f"no {prefix}- definition rows found in {rel}")
        defined |= found

    adr_dir = ROOT / config["adr_dir"]
    adr = {m.group(1) for p in sorted(adr_dir.glob("*.md"))
           for m in [re.search(r"^#\s+(ADR-\d{3,4})\b", p.read_text(encoding="utf-8"), re.M)] if m}
    if not adr:
        rep.fail(f"no ADR definition (a '# ADR-NNNN ' heading) found in {config['adr_dir']}")
    defined |= adr

    ev_dir = ROOT / config["evidence_dir"]
    declared = {p.stem.split("-")[0] + "-" + p.stem.split("-")[1]
                for p in ev_dir.glob("EV-*") if re.match(r"EV-\d+", p.name) and "-" in p.stem}
    defined |= declared
    return defined


def scan_files(config: dict) -> list[Path]:
    skip = set(config["skip_dirs"])
    suffixes = tuple(config["scan_suffixes"])
    files = [p for p in ROOT.rglob("*")
             if p.is_file() and p.suffix in suffixes and not (skip & set(p.relative_to(ROOT).parts))]
    for extra in config["scan_extra_files"]:
        extra_path = ROOT / extra
        if extra_path.is_file() and extra_path not in files:
            files.append(extra_path)
    return sorted(set(files))


def check_ids(rep: Report, files: list[Path], defined: set[str], ref: re.Pattern[str],
              exempt: list[str]) -> None:
    unresolved: dict[str, set[str]] = {}
    total = 0
    for path in files:
        for token in ref.findall(path.read_text(encoding="utf-8")):
            total += 1
            if token in defined or any(token.startswith(p) for p in exempt):
                continue
            unresolved.setdefault(token, set()).add(str(path.relative_to(ROOT)))
    if unresolved:
        rep.fail(f"{len(unresolved)} unresolved ID reference(s) of {total}",
                 [f"{token}  <- {', '.join(sorted(paths))}" for token, paths in sorted(unresolved.items())])
    else:
        rep.ok(f"IDs: {len(defined)} definitions, {total} references, 0 unresolved")


def check_placeholders(rep: Report, files: list[Path], prefixes: list[str],
                       extra: list[str]) -> None:
    alts = "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True))
    words = "|".join(re.escape(w) for w in extra)
    pattern = re.compile(rf"\b(?:{alts})-[xX]+\b|\b(?:{words})\b")
    hits = [f"{path.relative_to(ROOT)}:{n}: {m.group(0)}"
            for path in files
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if (m := pattern.search(line))]
    if hits:
        rep.fail(f"{len(hits)} placeholder(s)", hits)
    else:
        rep.ok("placeholders: none")


BUDGET_ROW = re.compile(
    r"^\|\s*`(?P<path>[^`]+)`\s*\|\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>B|KB|MB)\s*\|", re.M)
UNITS = {"B": 1, "KB": 1024, "MB": 1024 * 1024}


def parse_budgets(rep: Report, config: dict) -> list[tuple[str, int]]:
    page = ROOT / config["standard_page"]
    if not page.exists():
        rep.fail(f"standard page missing: {config['standard_page']} (the gate reads budgets from it)")
        return []
    rows = [(m.group("path").strip(), int(float(m.group("num")) * UNITS[m.group("unit")]))
            for m in BUDGET_ROW.finditer(page.read_text(encoding="utf-8"))]
    if not rows:
        rep.fail(f"no budget table rows found in {config['standard_page']} (expected '| `path` | 4 KB |')")
    return rows


def check_budgets(rep: Report, files: list[Path], budgets: list[tuple[str, int]]) -> None:
    overs: list[str] = []
    worst = (0.0, "no file matched a budget row")
    checked = 0
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        limits = [limit for pattern, limit in budgets if fnmatch.fnmatch(rel, pattern)]
        if not limits:
            continue
        limit = min(limits)
        size = path.stat().st_size
        checked += 1
        if size / limit > worst[0]:
            worst = (size / limit, f"{rel} {size}/{limit} B")
        if size > limit:
            overs.append(f"{rel}: {size} > {limit} B (over by {size - limit} B)")
    if overs:
        rep.fail(f"{len(overs)} file(s) over budget", overs)
    else:
        rep.ok(f"budgets: {checked} file(s) checked, highest usage {worst[0]:.0%} ({worst[1]})")


def coverage_rows(path: Path, prefix: str) -> list[tuple[str, str]]:
    """(id, full row text) for definition rows of one prefix."""
    pattern = re.compile(rf"^\|\s*(?P<id>{re.escape(prefix)}-[A-Z0-9-]*\d+)\s*\|.*$", re.M)
    if not path.exists():
        return []
    return [(m.group("id"), m.group(0)) for m in pattern.finditer(path.read_text(encoding="utf-8"))]


def check_coverage(rep: Report, config: dict, ref: re.Pattern[str]) -> None:
    req_prefix = config["coverage"]["requirement"]
    ac_prefix = config["coverage"]["acceptance"]
    req_path = ROOT / config["id_sources"][req_prefix]
    ac_path = ROOT / config["id_sources"][ac_prefix]
    req_rows = coverage_rows(req_path, req_prefix)
    ac_ids = [row_id for row_id, _ in coverage_rows(ac_path, ac_prefix)]
    referenced: set[str] = set()
    without_ac: list[str] = []
    for row_id, row in req_rows:
        linked = {t for t in ref.findall(row) if t.startswith(ac_prefix + "-")}
        if linked:
            referenced |= linked
        else:
            without_ac.append(row_id)
    exempt = config["exempt_prefixes"]
    orphans = [a for a in ac_ids if a not in referenced and not any(a.startswith(p) for p in exempt)]
    problems = []
    if without_ac:
        problems.append(f"{req_prefix} without an {ac_prefix}: " + ", ".join(without_ac))
    if orphans:
        problems.append(f"{ac_prefix} referenced by no {req_prefix}: " + ", ".join(orphans))
    if problems:
        rep.fail("coverage", problems)
    else:
        rep.ok(f"coverage: {len(req_rows)} {req_prefix} all linked, {len(ac_ids)} {ac_prefix} without orphans")


def main(argv: list[str]) -> int:
    config = load_config()
    prefixes = list(config["id_sources"]) + [p for p in config.get("extra_prefixes", [])]
    ref = ref_pattern(prefixes)
    rep = Report()
    files = scan_files(config)
    defined = collect_definitions(rep, config)
    if any(f.startswith("definition source missing") or f.startswith("no ADR") for f in rep.failures):
        print(f"== documentation gate (root {ROOT}) ==")
        print("\n".join(rep.lines))
        print(f"RESULT: FAIL ({len(rep.failures)}) - fix the definitions before the rest can be judged")
        return 2
    check_ids(rep, files, defined, ref, config["exempt_prefixes"])
    check_placeholders(rep, files, prefixes, config["placeholder_extra"])
    check_budgets(rep, files, parse_budgets(rep, config))
    check_coverage(rep, config, ref)
    print(f"== documentation gate (root {ROOT}) ==")
    print(f"scan: {len(files)} file(s) with suffix(es) {', '.join(config['scan_suffixes'])}")
    print("\n".join(rep.lines))
    print(f"RESULT: {'PASS' if not rep.failures else 'FAIL (' + str(len(rep.failures)) + ')'}")
    if "--verbose" in argv or "-v" in argv:
        for line in rep.lines:
            if line.startswith("       - "):
                print(line)
    return 1 if rep.failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

# ADR-0001 Adopt the agent-handover repository standard

Status: accepted

## Problem

Work here is done by agents across sessions, and every session boundary loses context: a crash, a
compaction, or simply a new session. Without a file-based state protocol, each new session re-derives
what the previous one knew, and an unwritten agreement ("we decided X last round") cannot be checked.
Documentation written for humans also drifts: the same fact gets restated in three files until they
disagree.

## Decision

This repository uses the agent-handover standard:

1. State lives in files, never in a model's memory: `docs/work/handover.md` (the single next action),
   `.agents/state.json` (machine-readable), `docs/work/progress-checklist.md` (task status).
2. Every fact is defined once and referenced by ID (see `12-documentation-standard.md` §2).
3. Every acceptance criterion has an executable command and captured evidence in `docs/work/evidence/`.
4. `tools/check_docs.py` (driven by `tools/docgate.json`) enforces ID integrity, budgets parsed from
   this standard page, placeholder absence, and FR↔AC coverage. It is wired as `tools/verify.sh docs`.

## Consequences

Positive: a new session can resume from three small files; documentation drift becomes a failing
command instead of a review opinion; a task cannot be marked done without evidence.

Negative: writing a doc costs more (IDs, budgets, provenance markers), and an untracked fact is
invisible to the gate — the discipline only pays off if every change goes through the gate. Small
repos may find the ADR requirement heavy; it is deliberately kept to decisions that are expensive to
reverse.

## Alternatives rejected

| Option | Why rejected |
|---|---|
| Prose-only docs, no IDs | references rot silently; no machine check is possible |
| A single giant `docs/README.md` | hits a size wall, diffs become unreadable, no per-file budget |
| Rely on agent memory / session history | lost at the first compaction or crash — the failure this repo is designed against |

## Revisit conditions

If a second reader surface (published site, external API docs) is added, or if the gate's runtime cost
becomes noticeable in CI.

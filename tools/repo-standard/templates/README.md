# @PROJECT@

<!-- budget: 4096 bytes, hard. Checked by tools/verify.sh docs. -->

@ONELINE@

## Current state

@STATE@

## How to work here

```bash
tools/verify.sh docs          # documentation gate: IDs, budgets, coverage, placeholders
cat docs/work/handover.md     # where the last round stopped + the single next action
cat .agents/state.json        # machine-readable state (phase / next_task / blockers)
```

Round loop:

1. Read `docs/work/handover.md`, then `.agents/state.json`, then `docs/work/progress-checklist.md`.
2. Run `tools/verify.sh docs`; if it is red, fix that before anything else.
3. Pick the first `todo`/`doing` task; read the FR and AC it names.
4. Implement, then run the AC command and capture the raw output into `docs/work/evidence/EV-NNN-<AC-ID>.txt`.
5. Update `progress-checklist.md`, `handover.md`, `.agents/state.json`, and append one line to `.agents/sessions/`.
6. Commit, push, and read the remote refs back (`git ls-remote origin`).

## Reading order

```
docs/work/handover.md                     # always first
docs/design/00-overview.md                # what the system is, and what it is not
docs/design/12-documentation-standard.md  # budgets, ID system, writing rules — read before writing docs
docs/work/roadmap.md                      # phases and their gates
```

Rules live in [AGENTS.md](AGENTS.md) — the single source of truth for them.

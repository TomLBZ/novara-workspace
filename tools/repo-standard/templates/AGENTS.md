# AGENTS.md — @PROJECT@ agent rules

<!-- budget: 4096 bytes, hard. Checked by tools/verify.sh docs. -->

Rules live in exactly one place: **this file**. README says how to work here, `docs/` says what the
system is, `.agents/` holds machine-readable state. Never copy rule text into README, skills or
memory — duplication is what drifts once a context window is compacted.

## Phase

<!-- one paragraph: the phase, what "done" means now, where the spec lives -->
@PHASE@

## Rules

1. **No backward compatibility yet.** Code and config validate the current spec only — no legacy path,
   no legacy-only message.
2. **One fact, one place.** Each fact is defined once; everywhere else links to it by ID or relative
   path. Repeated prose is a defect, not redundancy.
3. **Every claim carries a source** — code path with line numbers, spec/paper, or a marker:
   `[assumption]` (needs field verification), `[second-hand]`, `[inferred]`. Unmarked = defect.
4. **Every acceptance criterion has executable evidence.** Command plus raw output summary land in
   `docs/work/evidence/`; only then may the AC be `passed`.
5. **Spec before code.** A task exists in `docs/work/progress-checklist.md` and names its FR and AC.
   No AC yet → write the AC first (a docs commit), never the implementation first.
6. **Decisions are append-only.** Protocol, data-model, trust-model or boundary changes get a new ADR;
   an accepted ADR is never rewritten — mark it `superseded by ADR-NNNN`.
7. **Budgets are hard.** Each file declares its budget in the header and the gate fails above it.
   Over budget: delete duplication → delete narration → split the file → raise it, saying why.
8. **One batch, one handover.** A round ends with `progress-checklist` + `handover.md` +
   `.agents/state.json` updated, then commit → push → read the remote refs back.
9. **The gate is code, not an opinion.** Never make it green by editing the gate or weakening an AC;
   changing the standard needs an ADR.

## Layout

```
docs/analysis/   source material read at code level, with file/line citations
docs/design/     the designed system; adr/ holds decision records
docs/work/       roadmap, FR, AC, progress checklist, handover, evidence
.agents/state.json   machine-readable state: phase / next task / last verification / blockers
.agents/skills/      agent procedures for THIS repo (takeover, implement task, write ADR)
.agents/sessions/    one line per round — an index, not a log
```

## Handover and recovery

Every round — and before any compaction or risky step — write `docs/work/handover.md`: phase, last
verification command and result, **the single next action**, invariants, blockers.
Recovery order: `handover.md` → `.agents/state.json` → `progress-checklist.md` → act.
Never trust a memory of the last session: read the files, run the gate, then work.

## Commits

`<type>(<scope>): <summary>`, type ∈ {docs, design, feat, fix, test, chore}. One commit, one thing.
Type and scope stay English even when the summary is not.

## Maintenance

- Only the user edits this file; an agent changes it only with explicit approval in the current session,
  stating which rule changed and why.
- One line per rule. No logs, no dates, no examples, no restating README or skills.
- Hard limit **4096 bytes** (declared above); the gate fails above it or if a heading disappears.

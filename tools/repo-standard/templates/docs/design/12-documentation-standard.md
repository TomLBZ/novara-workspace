# 12 Documentation standard (read this before writing any doc)

<!-- budget: 10 KB. Defines the budgets, the ID system and the writing rules; the rules themselves live in AGENTS.md. -->

## 1 Budgets (hard)

The gate parses this table, so it is the single source of truth — never hardcode a limit in the script.

| File | Budget |
|---|---|
| `AGENTS.md` | 4096 B |
| `README.md` | 4096 B |
| `docs/work/handover.md` | 1024 B |
| `docs/design/*.md` | 28 KB |
| `docs/analysis/*.md` | 24 KB |
| `docs/design/adr/*.md` | 8 KB |
| `docs/work/*.md` | 32 KB |
| `.agents/skills/*/SKILL.md` | 8 KB |

Over budget, in this order: **delete duplication → delete narration → split the file → raise the
budget** (a raised budget is stated in the commit message). A file that declares a tighter budget in
its own header keeps that one.

## 2 ID system

Cross-document references use IDs, never "see above" or a page number.

| Prefix | Meaning | Defined in |
|---|---|---|
| `FR-<area>-<NNN>` | functional requirement | `docs/work/functional-requirements.md` |
| `AC-<area>-<NNN>` | acceptance criterion (with an executable command) | `docs/work/acceptance-criteria.md` |
| `T-<NNN>` | implementation task | `docs/work/progress-checklist.md` |
| `V-<NNN>` | assumption awaiting field verification | `docs/work/functional-requirements.md` |
| `ADR-<NNNN>` | architecture decision | `docs/design/adr/` |
| `EV-<NNN>` | evidence entry | `docs/work/evidence/` |

A definition lives in exactly one file; every other mention is a reference. The gate fails on an
unresolved reference, so add the definition before you cite the ID.

## 3 Writing rules

1. One fact, one place. Everything else links or cites the ID.
2. Mark provenance: code path (with line numbers), spec/paper, `[second-hand]`, `[inferred]`,
   `[assumption]` (to be verified in the field). An unmarked assertion is a defect.
3. No narration. Do not record review process or "we considered"; the reasoning goes to an ADR.
4. Concrete nouns, machine-checkable wording: "reject the quote when its package version differs",
   not "handle version inconsistencies".
5. Tables over prose for anything enumerable — the readers include agents.
6. No decorative symbols: no emoji, no ASCII art, no shouted punctuation.
7. One paragraph per physical line, so diffs stay readable and machines can parse it.

## 4 When an ADR is required

Required (AGENTS.md rule 6): wire/format changes, data-model or ledger semantics, boundaries of the
frozen core, trust and approval model, writable surface of self-modification, implementation stack.

Not required: wording, task status, scenario data, typo fixes.

ADR status values: `proposed` / `accepted` / `superseded by ADR-NNNN`. A superseded ADR keeps its
text; the new ADR carries the new reasoning.

## 5 Pre-commit self-check (agent)

- [ ] Every referenced ID exists (the gate proves it)
- [ ] New facts carry a source or an `[assumption]` marker
- [ ] No file over its budget
- [ ] Protocol/data-model/boundary change → new ADR written
- [ ] `progress-checklist` and `handover` updated for this round
- [ ] Committed, pushed, and the remote refs read back

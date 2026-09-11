# Functional requirements

<!-- budget: 32 KB (docs/work/*.md). -->

One row per requirement. The gate checks that every FR row references at least one AC, and that every
AC is referenced by some FR. Replace the seed rows as the project grows; keep the `FR-` / `V-` prefixes.

## 1 Requirements

| ID | Requirement | Source | AC |
|---|---|---|---|
| FR-CORE-001 | The repository's documentation gate must run offline with a stdlib Python only and fail on ID, budget, coverage or placeholder violations. | `docs/design/12-documentation-standard.md` | AC-CORE-001 |

## 2 Assumptions awaiting field verification (V)

| ID | Assumption | How to verify | Status |
|---|---|---|---|
| V-001 | The people who receive the output accept the documented format without rework. | Ask two real recipients to consume one sample and record the corrections. | open |

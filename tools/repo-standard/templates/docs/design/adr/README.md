# Architecture decision records

<!-- budget: 8 KB (docs/design/adr/*.md). -->

One file per decision: `NNNN-<short-slug>.md`, numbered sequentially from `0001`.

```
# ADR-NNNN <one-line decision>
Status: proposed | accepted | superseded by ADR-NNNN

## Problem        why this must be decided now; what breaks if it is not
## Decision       the decision itself, in checkable wording
## Consequences   positive and negative (a missing negative means it was not reviewed)
## Alternatives rejected   table: option | why rejected
## Revisit conditions      what would make us look at this again
```

An `accepted` ADR is append-only: rewrite the reasoning only by writing a new ADR and marking the old
one `superseded by ADR-NNNN`. The first heading line must stay `# ADR-NNNN ...` — the gate reads it.

# Acceptance criteria

<!-- budget: 32 KB (docs/work/*.md). -->

One row per criterion: an executable command plus the assertion that makes it pass. An AC that cannot
be written as a command is either two ACs or a manual check — mark those `manual` and name the person
who signs them off.

| ID | Criterion | Command | Expected |
|---|---|---|---|
| AC-CORE-001 | Documentation gate green on a clean checkout | `tools/verify.sh docs` | exit 0, `RESULT: PASS`, 0 unresolved IDs, 0 placeholders, 0 budget overruns |

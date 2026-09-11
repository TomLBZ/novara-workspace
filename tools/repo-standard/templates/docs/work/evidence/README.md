# Evidence

<!-- budget: 32 KB (docs/work/*.md). -->

One file per verified criterion: `EV-<NNN>-<AC-ID>.txt`, containing the date, the branch/commit, the
exact command, and the raw output. An AC may be marked `passed` only after its evidence file exists.
Evidence is append-only: a re-run appends a new block, it does not overwrite the earlier one.

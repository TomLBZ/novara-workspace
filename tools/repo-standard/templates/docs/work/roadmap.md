# Roadmap

<!-- budget: 32 KB (docs/work/*.md). -->

Each phase has a gate in `tools/verify.sh`; an unreached gate exits `2` ("not applicable yet"), a
failed gate exits `1`. That distinction keeps "not built yet" out of the failure list.

| Phase | Goal | Gate | Exit 2 while | Exit 0 when |
|---|---|---|---|---|
| P0 | Walking skeleton: spec, gate and the first executable path | `tools/verify.sh g0` | no implementation exists yet | every P0 AC passes in a clean checkout |
| P1 | The smallest end-to-end flow that a real user can complete | `tools/verify.sh g1` | P0 is not green | all P1 ACs pass, regression of P0 ACs included |
| P2 | Hardening: failure paths, observability, the first real user | `tools/verify.sh g2` | P1 is not green | all P2 ACs pass, plus the field-verification list resolved |

Rule: a phase gate is implemented only when its ACs exist — an AC-driven gate, never a hand-rolled
checklist.

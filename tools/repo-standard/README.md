# repo-standard — the agent-handover repository standard

<!-- budget: 4 KB. -->

The runnable form of the standard: a repo skeleton (`templates/`) plus a stdlib-only documentation gate
(`scripts/check_docs.py`) and its proof (`scripts/selftest.sh`). It is tracked here, in the portable
workspace, so a fresh machine that bind-mounts `/workspace` alone gets it back.

```bash
/workspace/tools/repo-standard/scripts/init_repo.sh <target-dir> <project-name>
cd <target-dir> && tools/verify.sh docs          # expect RESULT: PASS
/workspace/tools/repo-standard/scripts/selftest.sh   # 8/8 expectations held
```

The procedure, the design rationale and the pitfalls live in the Hermes skill `agent-handover-repo`
(`$(hermes home)/skills/software-development/agent-handover-repo/SKILL.md`); this directory is the code it
runs. First real instance: `/workspace/projects/quotagent`.

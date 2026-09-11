---
name: takeover
description: Use when resuming this repo after a crash, a context compaction, or in a new session. Rebuilds state from files and verifies before acting.
---

# Takeover and recovery

## Steps (in order)

1. **Read the handover** — `docs/work/handover.md` (≤1024 B) is the only source of the *single next action*.
2. **Read the state** — `.agents/state.json` (phase / next_task / last_verified / blockers).
3. **Read the checklist** — `docs/work/progress-checklist.md`; take the first `doing`, else the first `todo`.
   A `doing` row with no evidence and no owner counts as not started: redo it.
4. **Do not trust memory.** Anything from outside this session is true only if the repository says so.
5. **Verify before acting** — run `tools/verify.sh docs`. Red? Fix the gate's complaint first; that is the whole point of having it.
6. **Check the remote** — `git fetch origin && git log --oneline -3 origin/main`; compare with local HEAD. Behind: `git pull --rebase`. Never force-push.
7. **Claim the task** — set its status to `doing` and append one line to `.agents/sessions/README.md`.

## Facts that make recovery work

- The files are the authority: `handover.md` → `.agents/state.json` → `progress-checklist.md` are
  mutually redundant, and when they disagree `handover.md` wins and the other two get corrected at once.
- Never assume the last commit landed: confirm with `git ls-remote origin` and read the ref back.
- Anything not written down before the context ended is gone; write the handover before risky steps,
  not after them.

## Handover (end of every round)

1. `progress-checklist.md` — status + the EV file that proves it.
2. `handover.md` — phase, last verification command and result, the single next action, blockers.
3. `.agents/state.json` — next_task / last_verified_* / blockers.
4. Append one line to `.agents/sessions/README.md`.
5. `git add -A && git commit && git push`, then read the remote refs back.

## Anti-patterns (seeing one means: go back)

- Marking a task `done` without an evidence file.
- Continuing from "what I probably did last round".
- Making a red gate green by editing the gate or weakening the AC.
- Writing the handover as prose journal; it is a one-page executable instruction.

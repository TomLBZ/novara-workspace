# AGENTS.md — iron rules for this workspace

<!-- budget: 2048 bytes, hard. Enforced by tools/verify.sh (section 8b). -->

Read this before acting here. Rules live in exactly one place: **this file** holds
the rules, memory keeps a one-line pointer, README holds the how-to. Do not copy
rule text into memory, README or skills — duplication is what drifts.

## Rules

1. Early stage: **no backward compatibility**. Code and config validate the current
   spec only — never keep a legacy code path or a legacy-specific message.
2. **Never sweep `reasoning_effort` levels to compare token consumption.** Take the
   accepted vocabulary from the API's own error message or docs; at most one off/on
   A/B, and only when a switch is suspected of being silently ignored.
3. **Self-contained**: the workspace reproduces from a bind mount alone — no host
   installs, no writes to `$HOME`. Secrets only in `config.yaml` (0600, gitignored).
4. **Config surface = what the user must know**: one knob per concept, uniform field
   sets, no protocol details, no measurement noise. Runtimes are rebuilt with
   `tools/bootstrap.sh`, never committed.
5. **Finish a batch**: commit, push, then read the remote refs back.
6. **Public egress**: `novara.remoteblossom.com` is the only approved public address; new services are path prefixes behind `ws-gateway`, never a new host port.
7. **Repo scope**: track the environment and its system services; `projects/**` and project-owned services stay untracked, each project in its own repo.

## Maintenance

- Only the user changes this file. An agent edits it solely with explicit approval in
  the current session, and says which rule changed and why.
- One line per rule; no logs, no dates, no examples, no restating README or skills.
  Growth belongs there, not here.
- Hard limit **2048 bytes** (declared in the comment above). `tools/verify.sh` fails
  above it, or if the `## Rules` / `## Maintenance` headings disappear.

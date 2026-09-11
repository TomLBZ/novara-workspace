#!/usr/bin/env python3
"""wsconfig - read the workspace YAML configuration (secrets, keys, runtime paths).

Single source of truth: ``<workspace>/config.yaml``.

Examples
--------
    ws-config show                       # redacted overview
    ws-config get git.identity.email     # one value by dot-path
    ws-config get api_keys.openai.value --reveal
    ws-config export                     # shell exports (eval-able)
    ws-config env-file .env              # write a .env file (chmod 600)
    ws-config git-setup                  # render config/gitconfig + credentials store
    ws-config ssh-setup                  # render config/ssh_config from git.credentials[*].ssh_key
    ws-config llm [--json] [--request]   # resolved model + reasoning settings
    ws-config validate                   # schema / placeholder check

Python API
----------
    from wsconfig import load, get
    data = load()
    get(data, "api_keys.openai.value")
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shlex
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit(
        "wsconfig: PyYAML is missing.\n"
        "Fix: source <workspace>/bin/activate.sh && uv pip install pyyaml"
    )

SECRET_HINTS = ("token", "key", "password", "secret", "value", "pass", "webhook")

#: The one reasoning knob is `reasoning_effort`.  Values are compared against
#: the provider's supported list; these are the generic levels, weakest first,
#: used to snap a request down to the nearest supported one (xhigh/ultra/...).
LLM_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "ultra")
#: reasoning_effort values (or absent/empty) that mean "do not think at all"
LLM_EFFORT_OFF = (None, "", "none", "null", "off", "false", "disabled")
LLM_KINDS = ("openai-compatible", "anthropic", "gemini")

#: openai-compatible convention, measured against the live DeepSeek endpoint;
#: a provider block overrides these when its API spells things differently.
DEFAULT_EFFORT_PARAM = "reasoning_effort"
DEFAULT_DISABLED_BODY = {"thinking": {"type": "disabled"}}
#: obsolete per-provider keys, rejected by `ws-config validate`
_LEGACY_REASONING_KEYS = ("on_value", "off_value", "on", "off", "effort_param",
                          "levels", "verified", "effort_verified")


def workspace_root() -> Path:
    env = os.environ.get("WS_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def config_path() -> Path:
    return Path(os.environ.get("WS_CONFIG") or (workspace_root() / "config.yaml"))


def load(path: Path | str | None = None) -> dict:
    p = Path(path) if path else config_path()
    if not p.exists():
        sys.exit(f"wsconfig: config file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        sys.exit(f"wsconfig: {p} must contain a YAML mapping at top level")
    return data


def get(data, dotted: str, default=None):
    """Fetch a nested value with a dotted path (list indices allowed)."""
    cur = data
    for part in str(dotted).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur


def redact(value, key_name: str = ""):
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key_name) for v in value]
    if isinstance(value, str) and value:
        low = key_name.lower()
        if any(h in low for h in SECRET_HINTS):
            shown = (value[:4] + "…" + value[-2:]) if len(value) > 10 else "•••"
            return f"<set:{shown} len={len(value)}>"
    return value


def is_placeholder(value) -> bool:
    """True when the value still needs to be filled in."""
    if isinstance(value, dict):
        vals = [v for k, v in value.items() if k not in ("env", "base_url", "id", "host", "protocol", "notes")]
        return all(is_placeholder(v) for v in vals) if vals else True
    if isinstance(value, list):
        return all(is_placeholder(v) for v in value)
    if isinstance(value, str):
        s = value.strip()
        return s == "" or s.startswith("<") or s.lower() in {"changeme", "todo", "xxx", "none"}
    return value is None


def effort_state(raw=None, supported=None, mapping=None, param=None,
                 disabled_body=None) -> dict:
    """Resolve one ``reasoning_effort`` value into what goes on the wire.

    ``none`` / ``null`` / empty / an absent key -> reasoning off: the provider's
    ``disabled_body`` is sent instead of the effort field.  Anything else is sent
    verbatim when the provider lists it as supported, mapped through ``map``, or
    snapped down to the nearest supported level, so levels a provider does not
    have (xhigh, ultra, ...) still work instead of being rejected.
    """
    supported_l = [str(x).strip().lower() for x in (supported or [])]
    mapping_l = {str(k).strip().lower(): str(v).strip() for k, v in (mapping or {}).items()}
    if disabled_body is None:
        body_off: dict = {}
    elif isinstance(disabled_body, dict):
        body_off = disabled_body
    else:
        body_off = dict(DEFAULT_DISABLED_BODY)
    state = {"param": str(param).strip() if param else DEFAULT_EFFORT_PARAM,
             "disabled_body": body_off, "supported": supported_l, "raw": raw,
             "enabled": False, "level": None, "value": None,
             "mapped_from": None, "unknown": False}
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in LLM_EFFORT_OFF):
        return state
    value = str(raw).strip().lower()
    state["enabled"] = True
    if not supported_l or value in supported_l:
        state.update(level=value, value=value)
        return state
    if value in mapping_l:
        state.update(level=value, value=mapping_l[value], mapped_from=value)
        return state
    ranked = [x for x in supported_l if x in LLM_EFFORT_ORDER]
    weakest = ranked[0] if ranked else supported_l[0]
    if value not in LLM_EFFORT_ORDER:
        state.update(level=value, value=weakest, unknown=True)
        return state
    cut = LLM_EFFORT_ORDER.index(value)
    below = [x for x in ranked if LLM_EFFORT_ORDER.index(x) <= cut]
    state.update(level=value, value=(below[-1] if below else weakest), mapped_from=value)
    return state


def llm_settings(provider: str | None = None, model: str | None = None,
                 effort: str | None = None, data: dict | None = None) -> dict:
    """Resolve the effective LLM settings from config.yaml.

    Precedence: explicit argument > api_keys.<provider>.reasoning_effort >
    llm.reasoning_effort.  Returns a plain dict, safe to print (the secret
    itself is never included, only ``api_key_set``).
    """
    data = data if data is not None else load()
    llm = get(data, "llm", {}) or {}
    prov = (provider or get(llm, "provider") or "").strip()
    entry = get(data, f"api_keys.{prov}", {}) or {}
    if not isinstance(entry, dict):
        entry = {}
    has_key = bool(entry.get("value")) and not is_placeholder(entry.get("value"))
    models = [m.get("id") if isinstance(m, dict) else str(m) for m in (entry.get("models") or [])]
    chosen = (model or get(llm, "model") or entry.get("default_model") or
              (models[0] if models else ""))
    rb = entry.get("reasoning") if isinstance(entry.get("reasoning"), dict) else {}
    if effort is not None:
        raw_effort = effort
    elif "reasoning_effort" in entry:
        raw_effort = entry.get("reasoning_effort")
    else:
        raw_effort = llm.get("reasoning_effort")
    reasoning = effort_state(raw_effort, rb.get("supported"), rb.get("map"),
                             rb.get("param"),
                             rb.get("disabled_body", DEFAULT_DISABLED_BODY))
    req = dict(get(llm, "request", {}) or {})
    req.update(entry.get("options") or {})
    extra = dict(get(llm, "extra_body", {}) or {})
    extra.update(entry.get("extra_body") or {})
    return {
        "provider": prov,
        "kind": entry.get("kind") or "openai-compatible",
        "base_url": entry.get("base_url") or "",
        "env": entry.get("env") or f"{prov.upper()}_API_KEY" if prov else "",
        "api_key_set": has_key,
        "model": chosen,
        "models": models,
        "fallbacks": list(get(llm, "fallbacks", []) or []),
        "reasoning": reasoning,
        "request": {k: v for k, v in req.items() if v is not None},
        "extra_body": extra,
    }


def build_chat_request(messages, settings: dict | None = None, *,
                       stream: bool = False, model: str | None = None) -> dict:
    """Chat-completions body for ``settings``, reasoning included.

    Reasoning is one knob: ``reasoning_effort``.  Off means the provider's
    disabled body is sent, on means one effort field with a supported value.
    """
    st = settings or llm_settings()
    body: dict = {"model": model or st.get("model"), "messages": messages,
                  "stream": bool(stream)}
    for key, value in (st.get("request") or {}).items():
        if key in ("timeout_s", "retries"):
            continue
        body[key] = value
    r = st.get("reasoning") or {}
    if r.get("enabled"):
        body[r.get("param") or DEFAULT_EFFORT_PARAM] = r.get("value")
    else:
        body.update(r.get("disabled_body") or {})
    body.update(st.get("extra_body") or {})
    return body


def cmd_llm(args) -> int:
    """Print the resolved model / reasoning settings."""
    st = llm_settings(provider=args.provider, model=args.model, effort=args.effort)
    if args.json or args.request:
        payload = {"settings": st}
        if args.request:
            payload["request_body"] = build_chat_request(
                [{"role": "user", "content": "<prompt>"}], st)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    r = st.get("reasoning") or {}
    print(f"provider     : {st['provider'] or '<unset>'}"
          f"{'  (API key set)' if st['api_key_set'] else '  (API key MISSING)'}")
    print(f"kind         : {st['kind']}")
    print(f"base_url     : {st['base_url'] or '<provider default>'}")
    print(f"model        : {st['model'] or '<unset>'}")
    if st["models"]:
        print(f"models       : {', '.join(str(m) for m in st['models'])}")
    if st["fallbacks"]:
        print(f"fallbacks    : {', '.join(str(f) for f in st['fallbacks'])}")
    if r.get("enabled"):
        detail = f"{r['param']}={r['value']}"
        if r.get("mapped_from") and r["mapped_from"] != r["value"]:
            detail += f"   ({r['mapped_from']} -> {r['value']}: nearest supported level)"
        if r.get("unknown"):
            detail += f"   (UNKNOWN level '{r['level']}' - supported: {r['supported']})"
        print(f"reasoning    : on   {detail}")
    else:
        sent = json.dumps(r.get("disabled_body") or {}, ensure_ascii=False)
        print(f"reasoning    : off   (value {r.get('raw')!r} -> sends {sent})")
    print(f"request      : {json.dumps(st['request'], ensure_ascii=False)}")
    if st["extra_body"]:
        print(f"extra_body   : {json.dumps(st['extra_body'], ensure_ascii=False)}")
    return 0


def cmd_show(args) -> int:
    print(f"# {config_path()}")
    print(yaml.safe_dump(redact(load()), sort_keys=False, allow_unicode=True).rstrip())
    return 0


def cmd_get(args) -> int:
    value = get(load(), args.dotpath)
    if value is None:
        print(f"wsconfig: no such path: {args.dotpath}", file=sys.stderr)
        return 1
    if isinstance(value, (dict, list)):
        print(json.dumps(value if args.reveal else redact(value), indent=2, ensure_ascii=False))
    else:
        print(value)
    return 0


def _export_lines(only: set[str] | None = None) -> list[str]:
    data = load()
    out: list[str] = []
    keys = get(data, "api_keys", {}) or {}
    if isinstance(keys, dict):
        for name, entry in keys.items():
            if not isinstance(entry, dict):
                continue
            value = entry.get("value")
            env_var = entry.get("env") or f"{str(name).upper()}_API_KEY"
            if value and not is_placeholder(value):
                out.append(f"export {env_var}={shlex.quote(str(value))}")
            base = entry.get("base_url")
            if base and not is_placeholder(base):
                out.append(f"export {str(name).upper()}_BASE_URL={shlex.quote(str(base))}")
            mdl = entry.get("default_model")
            if mdl and not is_placeholder(mdl):
                out.append(f"export {str(name).upper()}_MODEL={shlex.quote(str(mdl))}")
    st = llm_settings(data=data)
    if st["provider"]:
        r = st.get("reasoning") or {}
        out += [
            f"export LLM_PROVIDER={shlex.quote(st['provider'])}",
            f"export LLM_MODEL={shlex.quote(str(st['model'] or ''))}",
            f"export LLM_REASONING_EFFORT="
            f"{shlex.quote('none' if not r.get('enabled') else str(r.get('value')))}",
            f"export LLM_REASONING_LEVEL="
            f"{shlex.quote(str(r.get('value') or ''))}",
        ]
        req = st.get("request") or {}
        if req.get("timeout_s") is not None:
            out.append(f"export LLM_TIMEOUT_S={shlex.quote(str(req['timeout_s']))}")
        if req.get("max_tokens") is not None:
            out.append(f"export LLM_MAX_TOKENS={shlex.quote(str(req['max_tokens']))}")
        if req.get("temperature") is not None:
            out.append(f"export LLM_TEMPERATURE={shlex.quote(str(req['temperature']))}")
    extra = get(data, "secrets", {}) or {}
    if isinstance(extra, dict):
        for name, value in extra.items():
            if value and not is_placeholder(value):
                out.append(f"export {str(name)}={shlex.quote(str(value))}")
    if only:
        out = [ln for ln in out if ln.split("=", 1)[0].replace("export ", "") in only]
    return out


def cmd_export(args) -> int:
    lines = _export_lines(set(args.only) if getattr(args, "only", None) else None)
    print("\n".join(lines))
    return 0


def cmd_env_file(args) -> int:
    lines = [ln.replace("export ", "", 1) for ln in _export_lines()]
    target = Path(args.path)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(0o600)
    print(f"wrote {target} ({len(lines)} vars, mode 600)")
    return 0


def cmd_git_setup(args) -> int:
    """Materialise config/gitconfig + config/git-credentials from config.yaml."""
    root = workspace_root()
    data = load()
    git = get(data, "git", {}) or {}
    identity = git.get("identity", {}) or {}
    creds = git.get("credentials", []) or []

    cred_lines, urls = [], []
    for cred in creds:
        if not isinstance(cred, dict):
            continue
        host, secret = cred.get("host"), cred.get("token") or cred.get("password")
        if not host or not secret or is_placeholder(secret):
            continue
        scheme = cred.get("protocol", "https")
        user = cred.get("username") or "git"
        cred_lines.append(f"{scheme}://{user}:{secret}@{host}")
        urls.append(f"{scheme}://{host}")

    cfg_path = Path(os.environ.get("GIT_CONFIG_GLOBAL") or root / "config" / "gitconfig")
    cred_path = root / "config" / "git-credentials"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    if cred_lines:
        cred_path.write_text("\n".join(cred_lines) + "\n", encoding="utf-8")
        cred_path.chmod(0o600)

    lines = [
        "# generated by `ws-config git-setup` from config.yaml - do not edit by hand",
        "[user]",
        f"\tname = {identity.get('name') or ''}",
        f"\temail = {identity.get('email') or ''}",
    ]
    if cred_lines:
        lines += ["[credential]", f"\thelper = store --file={cred_path}", "\tuseHttpPath = false"]
    for key, value in (git.get("config", {}) or {}).items():
        section, _, name = str(key).partition(".")
        lines += [f"[{section}]", f"\t{name} = {value}"]
    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cfg_path.chmod(0o600)
    print(f"wrote {cfg_path}")
    if cred_lines:
        print(f"wrote {cred_path} ({len(cred_lines)} credential(s): {', '.join(urls)})")
    else:
        print("no usable git credentials yet (config.yaml -> git.credentials)")
    return 0


def cmd_ssh_setup(args) -> int:
    """Materialise config/ssh_config (+ empty known_hosts) from config.yaml.

    Every credential carrying an ``ssh_key`` becomes a ``Host <host>`` block
    whose IdentityFile / UserKnownHostsFile are **workspace-local absolute
    paths**, so nothing depends on $HOME and ``ssh -F config/ssh_config`` keeps
    working after a move (activate.sh regenerates it when the recorded root
    differs).
    """
    root = workspace_root()
    data = load()
    creds = get(data, "git.credentials", []) or []

    entries: list[tuple[str, Path, str]] = []
    for cred in creds:
        if not isinstance(cred, dict):
            continue
        host = str(cred.get("host") or "").strip()
        key = str(cred.get("ssh_key") or "").strip()
        if not host or not key or is_placeholder(host) or is_placeholder(key):
            continue
        key_path = Path(key)
        if not key_path.is_absolute():
            key_path = root / key_path
        entries.append((host, key_path, str(cred.get("username") or "").strip()))

    cfg_path = root / "config" / "ssh_config"
    known_hosts = root / "config" / "ssh_known_hosts"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if not known_hosts.exists():
        known_hosts.write_text("", encoding="utf-8")
    known_hosts.chmod(0o600)

    lines = [
        "# generated by `ws-config ssh-setup` from config.yaml - do not edit by hand",
        f"# root: {root}",
        "# Consumed through GIT_SSH_COMMAND (bin/activate.sh) and bin/ws-ssh.",
        "# NOTE: `ssh -F` replaces the user ssh config - add Includes below if you",
        "#       need corporate ProxyJump / ProxyCommand settings.",
    ]
    for host, key_path, user in entries:
        lines += [
            "",
            f"Host {host}",
            f"\tIdentityFile {key_path}",
            "\tIdentitiesOnly yes",
            f"\tUserKnownHostsFile {known_hosts}",
            "\tStrictHostKeyChecking accept-new",
        ]
        if user:
            lines.append(f"#\tUser {user}   # advisory: url is ssh://{host}/..., user comes from the remote url")
    lines.append("")
    cfg_path.write_text("\n".join(lines), encoding="utf-8")
    cfg_path.chmod(0o644)

    if not getattr(args, "quiet", False):
        if entries:
            print(f"wrote {cfg_path} ({len(entries)} ssh host(s): {', '.join(h for h, _, _ in entries)})")
        else:
            print(f"wrote {cfg_path} (no ssh_key configured yet - config.yaml -> git.credentials[*].ssh_key)")
    return 0


def cmd_validate(args) -> int:
    data = load()
    problems: list[str] = []
    pending: list[str] = []
    notes: list[str] = []
    if not isinstance(data, dict) or not data.get("meta"):
        problems.append("missing top-level `meta` section")
    for name, entry in (get(data, "api_keys", {}) or {}).items():
        if not isinstance(entry, dict) or "value" not in entry:
            problems.append(f"api_keys.{name}: expected mapping with a `value` field")
            continue
        if is_placeholder(entry):
            pending.append(f"api_keys.{name}")
    for i, cred in enumerate(get(data, "git.credentials", []) or []):
        if not isinstance(cred, dict):
            problems.append(f"git.credentials[{i}]: expected a mapping (host/username/token)")
            continue
        if not cred.get("host") or is_placeholder(cred.get("host")):
            pending.append(f"git.credentials[{i}].host")
        if not cred.get("token") or is_placeholder(cred.get("token")):
            pending.append(f"git.credentials[{i}].token")
    for field in ("name", "email"):
        if is_placeholder(get(data, f"git.identity.{field}")):
            pending.append(f"git.identity.{field}")
    # --- llm / model settings -------------------------------------------------
    llm = get(data, "llm", {}) or {}
    if not isinstance(llm, dict):
        problems.append("`llm` must be a mapping")
        llm = {}
    prov = get(llm, "provider")
    if prov and prov not in (get(data, "api_keys", {}) or {}):
        problems.append(f"llm.provider '{prov}' is not a key of api_keys")
    elif not prov:
        pending.append("llm.provider")
    for i, fb in enumerate(get(llm, "fallbacks", []) or []):
        if fb not in (get(data, "api_keys", {}) or {}):
            problems.append(f"llm.fallbacks[{i}] '{fb}' is not a key of api_keys")
    if "reasoning" in llm:
        problems.append(
            "llm.reasoning is obsolete: the single knob is llm.reasoning_effort "
            "(none|null|'' disables thinking, otherwise a level such as medium)")
    effort_raw = llm.get("reasoning_effort")
    if effort_raw is not None and not isinstance(effort_raw, str):
        problems.append("llm.reasoning_effort must be a string (none|minimal|low|medium|high|...) "
                        "or null/empty to disable thinking")
    req = get(llm, "request", {}) or {}
    if not isinstance(req, dict):
        problems.append("llm.request must be a mapping")
    for num in ("timeout_s", "retries", "max_tokens", "temperature", "top_p"):
        val = req.get(num)
        if val is not None and not isinstance(val, (int, float)):
            problems.append(f"llm.request.{num} must be a number or null")
    for name, entry in (get(data, "api_keys", {}) or {}).items():
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind and kind not in LLM_KINDS:
            problems.append(f"api_keys.{name}.kind '{kind}' not in {list(LLM_KINDS)}")
        dm = entry.get("default_model")
        ids = [m.get("id") if isinstance(m, dict) else m for m in (entry.get("models") or [])]
        if dm and ids and dm not in ids:
            problems.append(f"api_keys.{name}.default_model '{dm}' not listed in .models {ids}")
        if ids and not dm:
            pending.append(f"api_keys.{name}.default_model")
        if "reasoning_effort" in entry and entry["reasoning_effort"] is not None                 and not isinstance(entry["reasoning_effort"], str):
            problems.append(f"api_keys.{name}.reasoning_effort must be a string or null")
        rb = entry.get("reasoning")
        if rb is not None and not isinstance(rb, dict):
            problems.append(f"api_keys.{name}.reasoning must be a mapping "
                            "(param / supported / map / disabled_body)")
            continue
        rb = rb or {}
        legacy = [k for k in _LEGACY_REASONING_KEYS if k in rb]
        if legacy:
            problems.append(
                f"api_keys.{name}.reasoning.{legacy[0]} is obsolete - a provider block only "
                "needs param / supported / map / disabled_body")
        if "param" in rb and not isinstance(rb["param"], str):
            problems.append(f"api_keys.{name}.reasoning.param must be a string")
        if "supported" in rb and not isinstance(rb["supported"], list):
            problems.append(f"api_keys.{name}.reasoning.supported must be a list of levels")
        if "map" in rb and not isinstance(rb["map"], dict):
            problems.append(f"api_keys.{name}.reasoning.map must be a mapping level -> provider value")
        if rb.get("disabled_body") is not None and not isinstance(rb.get("disabled_body"), dict):
            problems.append(f"api_keys.{name}.reasoning.disabled_body must be a mapping or null")
        if name == prov:
            provider_effort = entry["reasoning_effort"] if "reasoning_effort" in entry else effort_raw
            state = effort_state(provider_effort, rb.get("supported"), rb.get("map"),
                                 rb.get("param"),
                                 rb.get("disabled_body", DEFAULT_DISABLED_BODY))
            if state["unknown"]:
                problems.append(
                    f"reasoning_effort '{state['level']}' is not a known level and not in "
                    f"api_keys.{name}.reasoning.supported {state['supported']}")
            elif state["mapped_from"]:
                notes.append(f"reasoning_effort={state['mapped_from']} -> {state['value']} "
                             f"(nearest level supported by {name})")
            if not rb.get("supported"):
                notes.append(f"api_keys.{name}.reasoning.supported is empty - reasoning_effort "
                             "values are sent as-is (add the supported list to get validation "
                             "and mapping)")
    if problems:
        print("INVALID:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("config.yaml: structure OK")
    if notes:
        print(f"info ({len(notes)}):")
        for n in notes:
            print(f"  - {n}")
    if pending:
        print(f"empty / placeholder values ({len(pending)}) - fill in when available:")
        for p in pending:
            print(f"  - {p}")
    return 1 if problems else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ws-config", description="workspace config reader")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="print the config with secrets redacted").set_defaults(func=cmd_show)

    p = sub.add_parser("get", help="read one value by dot-path")
    p.add_argument("dotpath")
    p.add_argument("--reveal", action="store_true", help="do not redact secrets")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("export", help="print `export VAR=value` lines")
    p.add_argument("--only", nargs="*", help="restrict to these env var names")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("env-file", help="write a chmod-600 .env file")
    p.add_argument("path")
    p.set_defaults(func=cmd_env_file)

    sub.add_parser("git-setup", help="render config/gitconfig + git-credentials").set_defaults(
        func=cmd_git_setup
    )
    p = sub.add_parser("ssh-setup", help="render config/ssh_config from git.credentials[*].ssh_key")
    p.add_argument("--quiet", action="store_true", help="no output (used by activate.sh)")
    p.set_defaults(func=cmd_ssh_setup)
    p = sub.add_parser("llm", help="resolved provider / model / reasoning settings")
    p.add_argument("--provider", help="override llm.provider")
    p.add_argument("--model", help="override the model id")
    p.add_argument("--effort", help="override reasoning_effort (none disables thinking)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--request", action="store_true", help="also show the request body template")
    p.set_defaults(func=cmd_llm)
    sub.add_parser("validate", help="structure + placeholder check").set_defaults(func=cmd_validate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

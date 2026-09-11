#!/usr/bin/env python3
"""llm_probe - one on/off A/B check that the reasoning knob reaches the wire.

Deliberately small: the accepted level vocabulary comes from the API's own error
message / docs, and sweeping levels to compare token consumption is not done here
(it changes no configuration and costs money - see the llm-endpoint-parameter-probe
skill). What *is* worth one request pair: gateways commonly accept unknown JSON
fields with HTTP 200 and silently drop them, so "the request succeeded" never
proves a switch works. This script compares the observable difference instead:

  * off (``reasoning_effort: none``) must send the dialect's disabled body and
    come back with no ``reasoning_content`` / ``reasoning_tokens``
  * on (the configured level) must come back with reasoning, using a supported level

    python tools/llm_probe.py                 # A/B with the configured level
    python tools/llm_probe.py --effort high   # A/B with a specific level
    python tools/llm_probe.py --quick         # same as default (kept for verify.sh)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from wsconfig import build_chat_request, get, llm_settings, load  # noqa: E402

PROMPT = ("How many minutes are in 3.5 hours? Answer in one short line.")


def call(client, st, body):
    url = (st["base_url"] or "https://api.deepseek.com/v1").rstrip("/") + "/chat/completions"
    entry = get(load(), f"api_keys.{st['provider']}", {}) or {}
    key = os.environ.get(st.get("env") or "") or entry.get("value")
    t0 = time.time()
    r = client.post(url, headers={"Authorization": f"Bearer {key}"}, json=body)
    row = {"http": r.status_code, "seconds": round(time.time() - t0, 1)}
    if r.status_code == 200:
        d = r.json()
        msg = (d.get("choices") or [{}])[0].get("message", {}) or {}
        u = d.get("usage") or {}
        row.update(model=d.get("model"),
                   reasoning_tokens=(u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                   reasoning_chars=len(msg.get("reasoning_content") or ""),
                   sent_effort=body.get("reasoning_effort"))
    else:
        row["error"] = r.text[:200]
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--provider", help="api_keys entry to probe (default: llm.provider)")
    ap.add_argument("--model", help="model id (default: resolved model)")
    ap.add_argument("--effort", help="level for the 'on' half (default: the configured one)")
    ap.add_argument("--quick", action="store_true", help="accepted for compatibility; the default")
    ap.add_argument("--max-tokens", type=int, default=2000)
    args = ap.parse_args()

    base = llm_settings(provider=args.provider, model=args.model)
    if not base["api_key_set"]:
        print(f"no API key for provider '{base['provider']}' "
              f"(config.yaml -> api_keys.{base['provider']}.value)")
        return 2
    on_effort = args.effort or (base.get("reasoning") or {}).get("raw")
    if not on_effort:
        print("nothing to switch on: llm.reasoning_effort is off/empty "
              "(set a level, or pass --effort <level>)")
        return 2

    rows = []
    with httpx.Client(timeout=300.0) as client:
        for label, effort in (("off (reasoning_effort=none)", "none"), (f"on (effort={on_effort})", on_effort)):
            st = llm_settings(provider=args.provider, model=args.model, effort=effort)
            body = build_chat_request([{"role": "user", "content": PROMPT}], st)
            body["max_tokens"] = args.max_tokens
            row = {"case": label, "reasoning_effort": effort,
                   "sent": {k: v for k, v in body.items() if k not in ("messages", "model")},
                   **call(client, st, body)}
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)

    off, on = rows[0], rows[1]
    verdict = {
        "switch_works": off.get("http") == 200 and off.get("reasoning_chars") == 0
                        and off.get("reasoning_tokens") is None
                        and on.get("http") == 200,
        "off_sent": off.get("sent"),
        "on_sent": on.get("sent"),
        "on_reasoning_chars": on.get("reasoning_chars"),
        "note": "one A/B only - no level sweep (see the llm-endpoint-parameter-probe skill)",
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = ROOT / "logs" / f"llm-probe-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"settings": base, "rows": rows, "verdict": verdict},
                              indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nverdict:", json.dumps(verdict, ensure_ascii=False))
    print("evidence ->", out)
    return 0 if verdict["switch_works"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

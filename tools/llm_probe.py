#!/usr/bin/env python3
"""llm_probe - measure which reasoning knobs the configured endpoint really honours.

Why: several OpenAI-compatible gateways accept *any* unknown JSON field and ignore
it (verified on this workspace's endpoint: a junk parameter returns HTTP 200), so
"the request was accepted" proves nothing.  This script compares observable
effects instead, for the single reasoning knob ``reasoning_effort``:

  * ``none``/empty  -> must send the provider's disabled body; check that no
    ``reasoning_content`` / reasoning tokens come back at all
  * a level         -> how many reasoning tokens are actually spent

Results land in ``logs/llm-probe-<timestamp>.json``; the numbers quoted in
``config.yaml`` for ``api_keys.<provider>.reasoning.supported`` come from here.

    python tools/llm_probe.py                    # switch + every supported level (slow)
    python tools/llm_probe.py --quick            # switch only (2 calls)
    python tools/llm_probe.py --provider openai  # a different api_keys entry
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

PROMPT = (
    "Solve exactly and show the steps: a 3-machine job shop, jobs J1..J4 with routes "
    "(M1 3, M2 4), (M2 2, M3 5), (M1 6, M3 1), (M3 3, M1 2) minutes. Compute the optimal "
    "makespan by enumerating schedules, then state the best order."
)


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
        row.update(
            model=d.get("model"),
            reasoning_tokens=(u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            completion_tokens=u.get("completion_tokens"),
            reasoning_chars=len(msg.get("reasoning_content") or ""),
            finish=(d.get("choices") or [{}])[0].get("finish_reason"),
        )
    else:
        row["error"] = r.text[:200]
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--provider", help="api_keys entry to probe (default: llm.provider)")
    ap.add_argument("--model", help="model id (default: resolved model)")
    ap.add_argument("--quick", action="store_true", help="thinking switch only")
    ap.add_argument("--max-tokens", type=int, default=32000)
    args = ap.parse_args()

    base = llm_settings(provider=args.provider, model=args.model)
    if not base["api_key_set"]:
        print(f"no API key for provider '{base['provider']}' "
              f"(config.yaml -> api_keys.{base['provider']}.value)")
        return 2
    supported = list((base.get("reasoning") or {}).get("supported") or [])
    levels = ([lv for lv in supported] or ["medium"])
    cases = [("off (reasoning_effort=none)", "none")]
    if not args.quick:
        cases += [(f"effort={lv}", lv) for lv in levels]
        if "xhigh" not in levels:
            cases.append(("effort=xhigh (expect snap-down)", "xhigh"))

    rows = []
    with httpx.Client(timeout=600.0) as client:
        for label, effort in cases:
            st = llm_settings(provider=args.provider, model=args.model, effort=effort)
            body = build_chat_request([{"role": "user", "content": PROMPT}], st)
            body["max_tokens"] = args.max_tokens          # lift the cap, or every
            row = {"case": label, "reasoning_effort": effort,             # "high" run is clipped
                   "sent": {k: v for k, v in body.items() if k not in ("messages", "model")},
                   **call(client, st, body)}
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)

    by_level: dict[str, list] = {}
    for row in rows:
        if row.get("reasoning_tokens") is not None and row["case"].startswith("effort="):
            by_level.setdefault(row["reasoning_effort"], []).append(row["reasoning_tokens"])
    off_rows = [r for r in rows if r["case"].startswith("off")]
    verdict = {
        "switch_works": bool(off_rows) and off_rows[0].get("reasoning_chars") == 0
                         and off_rows[0].get("reasoning_tokens") is None,
        "reasoning_tokens": by_level,
    }
    means = {k: sum(v) // len(v) for k, v in by_level.items()}
    if means:
        verdict["means"] = means
        weak = [v for k, v in means.items() if k in ("minimal", "low")]
        strong = [v for k, v in means.items() if k in ("medium", "high", "xhigh", "ultra")]
        verdict["effort_works"] = bool(weak and strong and max(weak) < min(strong))
    if not base["base_url"]:
        verdict["note"] = "probed the provider default endpoint (base_url is empty)"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = ROOT / "logs" / f"llm-probe-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"settings": base, "rows": rows, "verdict": verdict},
                              indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nverdict:", json.dumps(verdict, ensure_ascii=False))
    print("evidence ->", out)
    return 0 if verdict.get("switch_works") else 1


if __name__ == "__main__":
    raise SystemExit(main())

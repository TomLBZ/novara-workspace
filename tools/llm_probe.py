#!/usr/bin/env python3
"""llm_probe - measure which reasoning knobs the configured endpoint really honours.

Why: several OpenAI-compatible gateways accept *any* unknown JSON field and
ignore it (verified on this workspace's endpoint: a junk parameter returns HTTP
200). So "the request was accepted" proves nothing about whether the parameter
does anything. This script compares the observable effect instead:

  * the thinking switch  -> is there any ``reasoning_content`` / reasoning_tokens?
  * the effort level     -> how many reasoning tokens are actually spent?

Results are written to ``logs/llm-probe-<timestamp>.json`` so the values in
``config.yaml`` (``api_keys.<provider>.reasoning.verified`` / ``effort_verified``)
can be re-checked after an endpoint or model change.

    python tools/llm_probe.py                    # full probe (a few minutes)
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

    st = llm_settings(provider=args.provider, model=args.model)
    r = st.get("reasoning") or {}
    if not st["api_key_set"]:
        print(f"no API key for provider '{st['provider']}' (config.yaml -> api_keys.{st['provider']}.value)")
        return 2
    if not r.get("supported"):
        print(f"provider '{st['provider']}' has no api_keys.{st['provider']}.reasoning block - "
              "nothing to probe (add param/on_value/off_value first)")
        return 2

    cases = [("switch off", {"enabled": False})]
    cases += [("switch on (no effort)", {"enabled": True, "effort": None})]
    if not args.quick:
        cases += [(f"effort={lvl}", {"enabled": True, "effort": lvl})
                  for lvl in ("minimal", "low", "medium", "high")]
    rows = []
    with httpx.Client(timeout=600.0) as client:
        for label, over in cases:
            s2 = json.loads(json.dumps(st))
            r2 = s2["reasoning"]
            r2["enabled"] = over["enabled"]
            if over.get("effort") is None:
                r2.pop("effort_param", None)          # switch only
            else:
                lvl = over["effort"]
                r2["effort"] = lvl
                r2["effort_value"] = (r2.get("levels") or {}).get(lvl, lvl)
            body = build_chat_request([{"role": "user", "content": PROMPT}], s2)
            body["max_tokens"] = args.max_tokens
            row = {"case": label, "params": body.get("thinking"), **call(client, s2, body)}
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)

    by_effort: dict[str, list] = {}
    for row in rows:
        if row.get("reasoning_tokens") is not None and "effort=" in row["case"]:
            by_effort.setdefault(row["case"], []).append(row["reasoning_tokens"])
    off = [x for x in rows if x["case"] == "switch off"]
    verdict = {
        "switch_works": bool(off) and off[0].get("reasoning_chars") == 0,
        "effort_tokens": by_effort,
    }
    if by_effort:
        vals = {k: sum(v) // len(v) for k, v in by_effort.items()}
        verdict["effort_means"] = vals
        lo = [v for k, v in vals.items() if k in ("effort=minimal", "effort=low")]
        hi = [v for k, v in vals.items() if k in ("effort=medium", "effort=high")]
        verdict["effort_works"] = bool(lo and hi and max(lo) < min(hi))
    if not st["base_url"]:
        verdict["note"] = "probed the provider default endpoint (base_url is empty)"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = ROOT / "logs" / f"llm-probe-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"settings": st, "rows": rows, "verdict": verdict},
                              indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nverdict:", json.dumps(verdict, ensure_ascii=False))
    print("evidence ->", out)
    return 0 if verdict.get("switch_works") else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Does a cache-routing key (`prompt_cache_key` / `user`) reach Fireworks
through Microsoft Foundry?

Nothing on either side documents this. Measure it.

  python3 eval/cache_key_probe.py                    # both fields, 12 trials each
  python3 eval/cache_key_probe.py --trials 25 --field prompt_cache_key

WHY THIS IS NOT A ONE-SHOT TEST

Header inspection is out: Azure APIM strips every `fireworks-*` response header
and substitutes its own `azureai-*` / `x-ratelimit-*` set. Only
`usage.prompt_tokens_details.cached_tokens` in the response body survives, so
routing has to be inferred from cache behaviour.

And a short sequence cannot do it. On a multi-replica PayGo pool a warm prefix
misses a meaningful fraction of the time purely from landing on a cold replica —
measured on this deployment, an identical prefix with NO key at all went
miss/HIT/HIT/miss, and with the SAME key throughout went miss/miss/HIT/HIT. A
single "different key missed!" observation is therefore worthless; it is
indistinguishable from routing noise.

THE DESIGN

Two arms, interleaved, each trial against a FRESH random prefix:

  arm SAME   call 1 key=A (warm)   ->  call 2 key=A   hit?
  arm DIFF   call 1 key=A (warm)   ->  call 2 key=B   hit?

Both arms pay the same cold-start and see the same replica churn. The only
difference is whether call 2 changes the key.

  SAME >> DIFF   the key partitions the cache -> it reached Fireworks
  SAME ~= DIFF   the key changes nothing -> dropped or ignored

A NULL_KEY arm runs alongside as the no-key baseline. Report the numbers, not a
single run's vibe.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HIT = 50.0  # percent of prompt tokens cached, above which we call it a hit


def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    for k in ("FOUNDRY_HOST", "AZURE_API_KEY", "FOUNDRY_MODEL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


_last_call = [0.0]


def call(url, api_key, model, marker, field, key, reps, min_interval, retries=4):
    """One request, paced and 429-aware.

    Foundry PayGo caps requests per minute per deployment (this one reports
    x-ratelimit-limit-requests: 66). Firing a probe flat out trips it, and a
    429 counted as a cache miss silently poisons the whole experiment — so
    pace, retry, and let the caller distinguish failure from miss.
    """
    body = {
        "model": model, "max_tokens": 8,
        "messages": [
            {"role": "system",
             "content": f"Marker {marker}. " +
                        "You are a reviewer. Check null derefs carefully. " * reps},
            {"role": "user", "content": "say ok"},
        ],
    }
    if field and key:
        body[field] = f"probe-key-{key}"

    for attempt in range(retries + 1):
        gap = time.time() - _last_call[0]
        if gap < min_interval:
            time.sleep(min_interval - gap)
        _last_call[0] = time.time()
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"api-key": api_key, "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                wait = float(e.headers.get("retry-after") or 0) or (5 * (attempt + 1))
                time.sleep(wait)
                continue
            raise
        if "error" in d:
            raise RuntimeError(str(d["error"])[:200])
        u = d.get("usage") or {}
        p = u.get("prompt_tokens", 0)
        c = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return (100.0 * c / p) if p else 0.0
    raise RuntimeError("rate limited after retries")


def trial(url, key, model, field, second_key, reps, min_interval):
    """Warm a fresh prefix with key A, then read it back. Returns hit bool.

    Raises on request failure so the caller can drop the trial rather than
    scoring it as a miss — a 429 is not a cache miss.
    """
    marker = uuid.uuid4().hex[:10]
    call(url, key, model, marker, field, "A", reps, min_interval)   # warm
    return call(url, key, model, marker, field, second_key, reps, min_interval) > HIT


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--field", action="append",
                    choices=["prompt_cache_key", "user"], default=None)
    ap.add_argument("--trials", type=int, default=12, help="trials per arm")
    ap.add_argument("--prefix-reps", type=int, default=300)
    ap.add_argument("--rpm", type=float, default=40.0,
                    help="pace requests to stay under the deployment's per-minute "
                         "limit (this one reports 66; default 40 leaves headroom)")
    args = ap.parse_args()

    env = load_env()
    host = env.get("FOUNDRY_HOST", "").split("://")[-1].split("/")[0].split(":")[0]
    api_key = env.get("AZURE_API_KEY", "")
    model = env.get("FOUNDRY_MODEL", "FW-GLM-5.2")
    if not host or not api_key:
        print("error: set FOUNDRY_HOST and AZURE_API_KEY in .env", file=sys.stderr)
        return 1
    url = f"https://{host}/openai/v1/chat/completions"
    fields = args.field or ["prompt_cache_key", "user"]
    n = args.trials

    print(f"  {url}\n  model={model}   {n} trials/arm   "
          f"({n * 2 * (len(fields) + 1)} calls total)\n")

    iv = 60.0 / args.rpm if args.rpm > 0 else 0.0
    est = (n * 2 * (len(fields) + 1)) * iv / 60.0
    print(f"  pacing at {args.rpm:.0f} req/min — about {est:.1f} min\n")

    def arm(field, second_key, label):
        """Run n trials; return (hits, completed). Failures are DROPPED, not
        counted as misses — a 429 is not a cache miss."""
        hits = done = 0
        for i in range(n):
            try:
                hits += trial(url, api_key, model, field, second_key,
                              args.prefix_reps, iv)
                done += 1
            except Exception as e:
                print(f"\n  {label} trial {i} dropped: {e}", file=sys.stderr)
            print("." if (i + 1) % 5 else f"{i+1}", end="", flush=True)
        return hits, done

    # No-key baseline: how often does a warm prefix hit with nothing set at all?
    print("── baseline: no key ──", flush=True)
    base, base_n = arm(None, None, "baseline")
    base_rate = 100.0 * base / base_n if base_n else 0.0
    print(f"\n  warm prefix hit rate with no key: {base_rate:.0f}%  "
          f"({base}/{base_n})\n")
    if base_n < max(3, n // 2):
        print("  too few completed baseline trials to compare against — "
              "lower --rpm or raise --trials\n", file=sys.stderr)
        return 1

    results = {}
    for field in fields:
        print(f"── field: {field} ──", flush=True)
        same, same_n = arm(field, "A", "SAME")
        diff, diff_n = arm(field, "B", "DIFF")
        if not same_n or not diff_n:
            print("  no completed trials; skipping\n", file=sys.stderr)
            continue
        s_rate, d_rate = 100.0 * same / same_n, 100.0 * diff / diff_n
        print()
        print(f"  same key  (A then A): {s_rate:5.0f}%  ({same}/{same_n})")
        print(f"  diff key  (A then B): {d_rate:5.0f}%  ({diff}/{diff_n})")
        gap = s_rate - d_rate
        # A real partition should push DIFF toward the cold-start rate while
        # SAME tracks the no-key baseline. Require a wide, unambiguous gap —
        # with n=12 anything under ~30 points is inside the noise.
        if gap >= 30 and d_rate < base_rate - 20:
            verdict = "REACHES Fireworks — the key partitions the cache"
        elif abs(gap) < 15:
            verdict = "NO EFFECT — key is dropped or ignored for cache routing"
        else:
            verdict = f"INCONCLUSIVE (gap {gap:+.0f} pts) — raise --trials"
        results[field] = (s_rate, d_rate, verdict)
        print(f"  -> {verdict}\n")

    print("═" * 64)
    print(f"  no-key baseline: {base_rate:.0f}% warm-prefix hit rate")
    for f, (s, d, v) in results.items():
        print(f"  {f:<18} same {s:3.0f}%  diff {d:3.0f}%   {v}")
    print()
    print("  A low baseline means this pool churns replicas; that alone costs")
    print("  cache hits and is what a routing key is supposed to fix.")
    print("  Azure APIM strips fireworks-* headers, so cache behaviour is the")
    print("  only observable. Post the numbers wherever the FAQ lives.")
    print("═" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

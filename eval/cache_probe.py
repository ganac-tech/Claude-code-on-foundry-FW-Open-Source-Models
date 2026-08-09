#!/usr/bin/env python3
"""Measure the REAL prompt-cache hit rate by querying Foundry directly.

The gateway cannot tell you this: Envoy AI Gateway v1.0.0 drops the upstream
OpenAI field `usage.prompt_tokens_details.cached_tokens` when it builds the
Anthropic response, so every request looks 100% uncached in gateway metrics.
This talks to Foundry's OpenAI endpoint directly, where the field survives.

  python3 eval/cache_probe.py                 # 4k-token prefix, 4 calls
  python3 eval/cache_probe.py --prefix-tokens 15000 --calls 5

Reads FOUNDRY_HOST / AZURE_API_KEY / FOUNDRY_MODEL from .env.

Interpretation: call 1 warms the cache, calls 2+ should report a high
cached_tokens. Sizing --prefix-tokens near your real workload matters —
Claude Code sends roughly 15k tokens of system prompt and tool definitions on
every turn, and that prefix is identical turn to turn, so it is exactly the
thing prompt caching should be absorbing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    for k in ("FOUNDRY_HOST", "AZURE_API_KEY", "FOUNDRY_MODEL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prefix-tokens", type=int, default=4000,
                    help="approximate size of the shared prefix (default 4000)")
    ap.add_argument("--calls", type=int, default=4)
    ap.add_argument("--price-in", type=float,
                    default=float(os.environ.get("GLM_PRICE_IN", "1.40")))
    ap.add_argument("--price-cached", type=float,
                    default=float(os.environ.get("GLM_PRICE_CACHED", "0.14")))
    args = ap.parse_args()

    env = load_env()
    host = env.get("FOUNDRY_HOST", "")
    key = env.get("AZURE_API_KEY", "")
    model = env.get("FOUNDRY_MODEL", "FW-GLM-5.2")
    host = host.split("://")[-1].split("/")[0].split(":")[0]
    if not host or not key:
        print("error: set FOUNDRY_HOST and AZURE_API_KEY in .env", file=sys.stderr)
        return 1

    # ~8 tokens per repetition of this sentence; close enough for a probe.
    prefix = "You are a code reviewer. Rule: check for null derefs. " * max(
        1, args.prefix_tokens // 11)
    url = f"https://{host}/openai/v1/chat/completions"

    print(f"  {url}")
    print(f"  model={model}  prefix~{args.prefix_tokens} tokens  calls={args.calls}\n")
    print(f"  {'call':<6}{'prompt':>9}{'cached':>9}{'hit%':>8}{'ms':>8}")

    rows = []
    for i in range(1, args.calls + 1):
        body = json.dumps({
            "model": model, "max_tokens": 8,
            "messages": [{"role": "system", "content": prefix},
                         {"role": "user", "content": f"Reply with the number {i}."}],
        }).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"api-key": key, "content-type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read())
        except Exception as e:
            print(f"  {i:<6}  error: {e}")
            continue
        ms = int((time.time() - t0) * 1000)
        u = d.get("usage", {})
        p = u.get("prompt_tokens", 0)
        c = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        rows.append((p, c))
        print(f"  {i:<6}{p:>9,}{c:>9,}{(100.0*c/p if p else 0):>7.1f}%{ms:>8,}")

    if len(rows) < 2:
        print("\n  not enough successful calls to judge caching", file=sys.stderr)
        return 1

    warm = rows[1:]                       # call 1 populates the cache
    wp = sum(p for p, _ in warm)
    wc = sum(c for _, c in warm)
    rate = 100.0 * wc / wp if wp else 0.0

    print(f"\n  steady-state hit rate (calls 2+): {rate:.1f}%")
    if rate < 5:
        print("  -> caching is NOT effective for this prefix shape.")
        return 0

    full = wp / 1e6 * args.price_in
    real = (wp - wc) / 1e6 * args.price_in + wc / 1e6 * args.price_cached
    print(f"  -> input cost on these {len(warm)} calls:")
    print(f"       billed as all-uncached : ${full:.6f}")
    print(f"       with cache             : ${real:.6f}"
          f"   ({100.0 * (1 - real/full) if full else 0:.0f}% lower)")
    print(f"\n  Feed this rate back into the gateway cost estimate:")
    print(f"    python3 eval/gateway_stats.py --assume-cached-pct {rate:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
